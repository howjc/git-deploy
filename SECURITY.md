# Security Policy

## Supported versions

Security fixes are considered for the latest release on the default branch (`main`). Older tags may not receive backports.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report privately via one of:

1. [GitHub Security Advisories](https://github.com/howjc/git-deploy/security/advisories/new) (preferred when available)
2. Contact the repository maintainers through a private channel linked from the GitHub profile of the owner (`howjc`)

Include:

- Affected version or commit
- Impact and reproduction steps (or a minimal proof of concept)
- Whether a fix or workaround is already known

You should receive an acknowledgement when the report is seen. Coordinated disclosure is preferred: please allow time for a fix or mitigation before public discussion.

## Scope notes

`git-deploy` is a local CLI that runs builds and uploads files over SFTP/FTP. It is **not** a hosted multi-tenant service. Reports that matter most:

- Credential handling (env-backed secrets, no plaintext password fields)
- Path traversal or unexpected remote path writes outside configured roots
- Hybrid ownership / recovery logic that could delete or overwrite unmanaged remote content contrary to documented safety boundaries
- Command injection via configuration fields that are intended only for user-reviewed shell steps

Out of scope unless they break the product’s own safety claims:

- Misconfiguration of user `deploy.toml`, SSH config, or `after_deploy` commands
- Compromised build steps the user chose to run locally
- General FTP/SFTP protocol limitations documented in the README security section
