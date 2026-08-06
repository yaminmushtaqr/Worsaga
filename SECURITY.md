# Security

Worsaga handles Moodle credentials and may access private course data.

## Reporting a vulnerability

Report security issues **privately**, through GitHub's private vulnerability
reporting: open the repository's **Security** tab and choose **Report a
vulnerability**. That opens a private thread visible only to the maintainer,
so a report can include enough detail to reproduce the problem without
publishing it first.

Please do not open a public GitHub issue for a security report, and in
particular never put tokens, credentials, private Moodle URLs, course data,
grades, messages, notifications, or downloaded materials in one — a public
issue cannot be un-published.

## Supported versions

Security fixes target the **latest release on PyPI**. Older versions are not
patched; if you are reporting against an older one, please check whether the
current release still has the problem.

## If you think your token has leaked

A Moodle web-service token is equivalent to your Moodle access. If one has
been exposed — committed to a repository, pasted into a chat or an issue,
left in a shell history, or read off a shared machine:

1. **Revoke it in Moodle first.** Profile → **Preferences** → **Security
   keys** → **Reset** on the relevant service. This is the only step that
   actually stops the token working; everything else is cleanup.
2. **Delete Worsaga's local data.** The configuration file holds the token,
   and the cache, index, and downloads hold course data. Every path is listed
   in [What Worsaga stores](README.md#what-worsaga-stores) in the README,
   with what it contains and how to remove it.
3. **Report it privately** — via the Security tab, as above — if you think
   Worsaga itself is at fault: if it wrote the token somewhere it should not
   have, printed it in output, or failed to redact it.
4. **Keep the report clean.** Never include the token itself, your Moodle
   URL, or course content in a public issue, and prefer a redacted
   reproduction even in a private report.

Bear in mind that backups, filesystem snapshots, and cloud file-sync clients
may still hold a copy of the configuration file after you delete it. That is
why step 1 comes first: revoking the token is what makes those copies
worthless.

## Token safety

- Never commit your Moodle token.
- Treat your Moodle token like a password. Worsaga never asks for your
  university password itself — see
  [Worsaga never asks for your password](README.md#worsaga-never-asks-for-your-password).
- Credentials are stored in a local plaintext JSON config file, with
  owner-only file permissions where the OS supports them. **Anyone with
  access to your OS user account can read it.** OS-keychain storage may be
  offered later.
- Prefer the guided `worsaga setup` flow.
- Do not paste `worsaga --json` output publicly if it includes course, grade,
  message, material, or internal Moodle information.

## Scope

Worsaga is read-only against Moodle and writes only to local stores on your
own machine. Reports about accidental write-like behaviour, token leakage,
unsafe file downloads, path traversal, or unexpected exposure of private
course data are in scope.

Running Worsaga as a shared, multi-user, remote, or hosted service is
explicitly unsupported, and reports that depend on such a deployment are out
of scope — see [One machine, one user](README.md#one-machine-one-user).
