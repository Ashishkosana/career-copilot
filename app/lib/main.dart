import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:url_launcher/url_launcher.dart';

import 'api.dart';
import 'auth.dart';
import 'models.dart';

// --- Providers -------------------------------------------------------------
final authProvider = Provider<Auth>((ref) => Auth());
final apiProvider = Provider<Api>((ref) => Api(ref.read(authProvider)));
final briefingProvider =
    FutureProvider.autoDispose<Briefing>((ref) => ref.read(apiProvider).briefing());

const _indigo = Color(0xFF4F46E5);
const _bg = Color(0xFFF7F7FB);

void main() => runApp(const ProviderScope(child: CopilotApp()));

class CopilotApp extends StatelessWidget {
  const CopilotApp({super.key});
  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'Career Copilot',
        debugShowCheckedModeBanner: false,
        theme: ThemeData(
          colorSchemeSeed: _indigo,
          scaffoldBackgroundColor: _bg,
          useMaterial3: true,
        ),
        home: const SignInPage(),
      );
}

// --- Sign in ---------------------------------------------------------------
class SignInPage extends ConsumerStatefulWidget {
  const SignInPage({super.key});
  @override
  ConsumerState<SignInPage> createState() => _SignInPageState();
}

class _SignInPageState extends ConsumerState<SignInPage> {
  // Public repo: no personal address prefilled. Pass one for local convenience
  // with `flutter run --dart-define=COPILOT_EMAIL=you@example.com`.
  final _email = TextEditingController(
    text: const String.fromEnvironment('COPILOT_EMAIL'),
  );
  final _password = TextEditingController();
  bool _loading = false;
  String? _error;

  Future<void> _signIn() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      await ref.read(authProvider).signIn(_email.text.trim(), _password.text);
      if (mounted) {
        Navigator.of(context)
            .pushReplacement(MaterialPageRoute(builder: (_) => const BriefingPage()));
      }
    } catch (e) {
      setState(() => _error = 'Sign-in failed. Check your email and password.');
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        body: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(28),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const Text('Career Copilot',
                    textAlign: TextAlign.center,
                    style: TextStyle(fontSize: 30, fontWeight: FontWeight.w800, color: _indigo)),
                const SizedBox(height: 6),
                const Text('Your daily job-search briefing',
                    textAlign: TextAlign.center, style: TextStyle(color: Colors.black54)),
                const SizedBox(height: 32),
                TextField(
                    controller: _email,
                    decoration: const InputDecoration(labelText: 'Email', border: OutlineInputBorder())),
                const SizedBox(height: 14),
                TextField(
                    controller: _password,
                    obscureText: true,
                    onSubmitted: (_) => _signIn(),
                    decoration:
                        const InputDecoration(labelText: 'Password', border: OutlineInputBorder())),
                const SizedBox(height: 20),
                FilledButton(
                  onPressed: _loading ? null : _signIn,
                  style: FilledButton.styleFrom(
                      backgroundColor: _indigo, padding: const EdgeInsets.symmetric(vertical: 16)),
                  child: _loading
                      ? const SizedBox(
                          height: 20,
                          width: 20,
                          child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
                      : const Text('Sign in'),
                ),
                if (_error != null)
                  Padding(
                      padding: const EdgeInsets.only(top: 16),
                      child: Text(_error!,
                          textAlign: TextAlign.center, style: const TextStyle(color: Colors.red))),
              ],
            ),
          ),
        ),
      );
}

// --- Briefing --------------------------------------------------------------
class BriefingPage extends ConsumerWidget {
  const BriefingPage({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final briefing = ref.watch(briefingProvider);
    return Scaffold(
      appBar: AppBar(
        title: const Text('Today', style: TextStyle(fontWeight: FontWeight.w700)),
        backgroundColor: _indigo,
        foregroundColor: Colors.white,
        elevation: 0,
        actions: [
          IconButton(
              tooltip: 'Refresh',
              icon: const Icon(Icons.refresh),
              onPressed: () => ref.invalidate(briefingProvider)),
        ],
      ),
      body: briefing.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (e, _) => _ErrorState(onRetry: () => ref.invalidate(briefingProvider)),
        data: (b) => RefreshIndicator(
          onRefresh: () async => ref.invalidate(briefingProvider),
          child: ListView(
            padding: const EdgeInsets.fromLTRB(16, 16, 16, 32),
            children: [
              _sectionHeader(Icons.notifications_active_outlined, 'Needs you',
                  const Color(0xFFDC2626)),
              if (b.needsAction.isEmpty)
                _EmptyCard(text: "You're all caught up.")
              else
                ...b.needsAction.map((a) => _ActionCard(item: a)),
              const SizedBox(height: 8),
              _sectionHeader(Icons.track_changes, "Today's matches", _indigo),
              if (b.jobs.isEmpty)
                _EmptyCard(text: 'No new roles today.')
              else
                ...b.jobs.map((j) => _JobCard(job: j)),
              if (b.pipeline.isNotEmpty) ...[
                const SizedBox(height: 8),
                _sectionHeader(Icons.bar_chart, 'Pipeline', const Color(0xFF6366F1)),
                _PipelineChips(pipeline: b.pipeline, scanned: b.scanned, noise: b.noise),
              ],
            ],
          ),
        ),
      ),
    );
  }

  Widget _sectionHeader(IconData icon, String text, Color color) => Padding(
        padding: const EdgeInsets.only(top: 12, bottom: 8, left: 4),
        child: Row(
          children: [
            Icon(icon, size: 20, color: color),
            const SizedBox(width: 8),
            Text(text, style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w700)),
          ],
        ),
      );
}

const _statusColors = <String, Color>{
  'offer': Color(0xFF059669),
  'interview': Color(0xFF059669),
  'assessment': Color(0xFFD97706),
  'applied': _indigo,
  'viewed': Color(0xFF6366F1),
  'rejected': Color(0xFF9CA3AF),
};

class _ActionCard extends StatelessWidget {
  const _ActionCard({required this.item});
  final ActionItem item;

  @override
  Widget build(BuildContext context) {
    final color = _statusColors[item.status] ?? _indigo;
    return Card(
      elevation: 0,
      margin: const EdgeInsets.symmetric(vertical: 5),
      shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(14), side: const BorderSide(color: Color(0xFFE5E7EB))),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
              decoration:
                  BoxDecoration(color: color.withValues(alpha: 0.12), borderRadius: BorderRadius.circular(20)),
              child: Text(item.status.toUpperCase(),
                  style: TextStyle(color: color, fontWeight: FontWeight.w700, fontSize: 11)),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(item.subject, style: const TextStyle(fontWeight: FontWeight.w600)),
                  const SizedBox(height: 2),
                  Text(item.who, style: const TextStyle(color: Colors.black54, fontSize: 13)),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _JobCard extends StatelessWidget {
  const _JobCard({required this.job});
  final JobMatch job;

  Future<void> _open() async {
    final uri = Uri.tryParse(job.url);
    if (uri != null) await launchUrl(uri, mode: LaunchMode.externalApplication);
  }

  @override
  Widget build(BuildContext context) {
    final sub = [job.company, if (job.location.isNotEmpty) job.location].join('  ·  ');
    return Card(
      elevation: 0,
      margin: const EdgeInsets.symmetric(vertical: 5),
      shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(14), side: const BorderSide(color: Color(0xFFE5E7EB))),
      child: InkWell(
        borderRadius: BorderRadius.circular(14),
        onTap: _open,
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              _ScoreBadge(score: job.score),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(job.title,
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(fontWeight: FontWeight.w700, height: 1.25)),
                    const SizedBox(height: 3),
                    Text(sub, style: const TextStyle(color: Colors.black54, fontSize: 13)),
                  ],
                ),
              ),
              const SizedBox(width: 8),
              const Icon(Icons.open_in_new, size: 18, color: _indigo),
            ],
          ),
        ),
      ),
    );
  }
}

class _ScoreBadge extends StatelessWidget {
  const _ScoreBadge({required this.score});
  final int score;
  @override
  Widget build(BuildContext context) => Container(
        width: 46,
        height: 46,
        alignment: Alignment.center,
        decoration: BoxDecoration(color: _indigo.withValues(alpha: 0.10), shape: BoxShape.circle),
        child: Text('$score%',
            style: const TextStyle(color: _indigo, fontWeight: FontWeight.w800, fontSize: 13)),
      );
}

class _PipelineChips extends StatelessWidget {
  const _PipelineChips({required this.pipeline, required this.scanned, required this.noise});
  final Map<String, int> pipeline;
  final int scanned;
  final int noise;

  @override
  Widget build(BuildContext context) {
    const order = ['offer', 'interview', 'assessment', 'viewed', 'applied', 'rejected'];
    final chips = [
      for (final s in order)
        if ((pipeline[s] ?? 0) > 0)
          Chip(
            label: Text('${pipeline[s]} $s'),
            backgroundColor: (_statusColors[s] ?? _indigo).withValues(alpha: 0.10),
            side: BorderSide.none,
            labelStyle: TextStyle(color: _statusColors[s] ?? _indigo, fontWeight: FontWeight.w600),
          ),
    ];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(spacing: 8, runSpacing: 4, children: chips),
        const SizedBox(height: 8),
        Text('${scanned - noise} job emails · $noise noise (of $scanned scanned)',
            style: const TextStyle(color: Colors.black45, fontSize: 12)),
      ],
    );
  }
}

class _EmptyCard extends StatelessWidget {
  const _EmptyCard({required this.text});
  final String text;
  @override
  Widget build(BuildContext context) => Container(
        width: double.infinity,
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(color: Colors.white, borderRadius: BorderRadius.circular(14)),
        child: Text(text, style: const TextStyle(color: Colors.black54)),
      );
}

class _ErrorState extends StatelessWidget {
  const _ErrorState({required this.onRetry});
  final VoidCallback onRetry;
  @override
  Widget build(BuildContext context) => Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.cloud_off, size: 40, color: Colors.black26),
            const SizedBox(height: 12),
            const Text("Couldn't load your briefing."),
            const SizedBox(height: 12),
            FilledButton(onPressed: onRetry, child: const Text('Retry')),
          ],
        ),
      );
}
