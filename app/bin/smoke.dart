// ignore_for_file: avoid_print, avoid_relative_lib_imports
// End-to-end smoke test: sign in via Cognito SRP, fetch the briefing.
// Proves auth + JWT + API Gateway authorizer + Lambda + DynamoDB all connect.
//
// This repository is public, so credentials are never defaulted in source.
// Supply them per run:
//
//   dart run bin/smoke.dart <email> <password>
//
// or via the environment:
//
//   COPILOT_EMAIL=you@example.com COPILOT_PASSWORD=... dart run bin/smoke.dart
import 'dart:io';

import '../lib/api.dart';
import '../lib/auth.dart';

Future<void> main(List<String> args) async {
  final email = args.isNotEmpty ? args[0] : Platform.environment['COPILOT_EMAIL'];
  final password = args.length > 1 ? args[1] : Platform.environment['COPILOT_PASSWORD'];

  if (email == null || email.isEmpty || password == null || password.isEmpty) {
    stderr.writeln(
      'Missing credentials.\n'
      '  dart run bin/smoke.dart <email> <password>\n'
      '  or set COPILOT_EMAIL and COPILOT_PASSWORD',
    );
    exitCode = 2;
    return;
  }

  final auth = Auth();
  print('Signing in...');
  await auth.signIn(email, password);
  print('Signed in: ${auth.isSignedIn}');
  final b = await Api(auth).briefing();
  print('needs-you: ${b.needsAction.length} · jobs: ${b.jobs.length}');
  for (final j in b.jobs.take(6)) {
    print('  ${j.score}%  ${j.title} @ ${j.company}');
  }
}
