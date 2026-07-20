# Security

Worsaga handles Moodle credentials and may access private course data.

Please do not report security issues by opening a public GitHub issue if the
report contains tokens, credentials, private Moodle URLs, course data, grades,
messages, notifications, or downloaded materials.

For now, contact the maintainer directly.

## Token safety

- Never commit your Moodle token.
- Treat your Moodle token like a password.
- Credentials are stored in a local plaintext JSON config file, with owner-only
  file permissions where the OS supports them. Anyone with access to your OS
  user account can read it. OS-keychain storage may be offered later.
- Prefer the guided `worsaga setup` flow.
- Do not paste `worsaga --json` output publicly if it includes course, grade,
  message, material, or internal Moodle information.

## Scope

Worsaga is read-only by design. Reports about accidental write-like behaviour,
token leakage, unsafe file downloads, path traversal, or unexpected exposure of
private course data are in scope.
