# git-deploy

Deploy the tracked file delta between two Git commits over SFTP, FTP, or FTPS.
The tool creates an exact pre-deployment backup and can restore it later by
deployment ID. Existing `deploy.py` remains the legacy full-upload command.

The new package lives under `src/git_deploy`; neither `deploy.py` nor its
`deploy.example.toml` configuration is imported or modified by the new CLI.

## Install

From a standalone `git-deploy` clone:

```bash
uv tool install --editable .
git-deploy --help
```

From the `baota-official` monorepo root:

```bash
uv tool install --editable ./scripts/deploy
```

For a non-editable standalone installation:

```bash
uv build
uv tool install .
```

## Configuration

`git-deploy` resolves its configuration in this order:

1. `--config PATH`
2. `./deploy.toml` in the current working directory
3. `GIT_DEPLOY_CONFIG`
4. `~/.config/git-deploy/deploy.toml`

It never searches parent directories. Relative paths inside the configuration
are resolved from the directory containing that configuration file.
Start from `git-deploy.example.toml`; keep the real `deploy.toml` untracked.

```toml
[server]
protocol = "sftp"
ssh_host_alias = "bt-official-prod"
ssh_config_file = "~/.ssh/config"
strict_host_key_checking = true

[projects.official]
repository = "."
remote_root = "/www/wwwroot/www.bt.cn"
include = ["app/**", "config/**", "public/**", "route/**", "extend/**"]
exclude = ["tests/**", "docs/**", "runtime/**", "tmp/**", ".env*"]
protected = [".env", "runtime/**", "app/storage/cert/**", "app/storage/enc/**"]
post_commands = [
  "cd /www/wwwroot/www.bt.cn && php think clear",
  "/etc/init.d/php-fpm-83 reload",
]
health_urls = ["https://www.bt.cn/api/oauth/jwks"]
```

For 1Password SSH Agent, use a public `IdentityFile` in `~/.ssh/config`:

```sshconfig
Host bt-official-prod
    HostName 192.0.2.10
    User deploy
    IdentityFile ~/.ssh/1password/bt-official-prod.pub
    IdentitiesOnly yes
```

The corresponding private key remains in the agent. The tool matches the
public-key fingerprint to one agent key instead of trying every loaded key.

## Usage

```bash
# Local-only preview; no server connection and no writes.
git-deploy deploy official --from COMMIT_A --to COMMIT_B --dry-run

# Equivalent dedicated preview command.
git-deploy plan official --from COMMIT_A --to COMMIT_B

# Read-only remote drift check; still performs no writes.
git-deploy deploy official --from COMMIT_A --to COMMIT_B \
  --dry-run --check-remote

# Apply a deployment.
git-deploy deploy official --from COMMIT_A --to COMMIT_B --yes

# Show local deployment history and restore the latest successful deployment.
git-deploy history official
git-deploy verify official --deployment DEPLOYMENT_ID
git-deploy rollback official --latest --dry-run
git-deploy rollback official --latest --yes
```

`all` selects every configured project. Different repositories require one
range per project:

```bash
git-deploy deploy all \
  --range official=COMMIT_A..COMMIT_B \
  --range backend=COMMIT_C..COMMIT_D \
  --dry-run
```

`--deployment` is the local deployment record ID printed after a successful
deployment. A unique prefix is accepted. `--latest` selects the newest record
whose status is still `succeeded`; `rollback all` therefore requires
`--latest` rather than one shared deployment ID.

## Safety model

- Target bytes are read from the target commit, never from the working tree.
- Uncommitted working-tree changes are ignored and reported before deployment.
- Modified and deleted remote files must match the source commit by SHA-256.
- A delete is idempotent when the remote path is already absent; no `--force` is needed.
- An upload is idempotent when the remote hash already equals the target commit and is skipped.
- Added files must be absent remotely unless `--force` is supplied.
- `.env`, private keys, runtime data, and configured protected paths are blocked.
- Uploads use temporary names followed by rename; deletes run last.
- Remote checks, backups, uploads, deletes, verification, and rollback report progress.
- Rollback restores the exact remote bytes captured before deployment.
- Rollback is code/file rollback only and does not reverse database migrations.
