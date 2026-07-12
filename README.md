# git-deploy

Deploy tracked file changes selected from Git commits over SFTP, FTP, or FTPS.
The tool accepts single commits, continuous ranges, and non-contiguous
combinations. It creates an exact pre-deployment backup and can restore it
later by deployment ID. Existing `deploy.py` remains the legacy full-upload
command.

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

For a single remote, the existing `[server]` form remains supported:

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

For multiple environments, name each connection under `[remotes.NAME]` and
put environment-specific project settings under `[projects.NAME.remotes.NAME]`:

```toml
[remotes.dev]
protocol = "sftp"
ssh_host_alias = "bt-official-dev"
ssh_config_file = "~/.ssh/config"
strict_host_key_checking = true

[remotes.prod]
protocol = "sftp"
ssh_host_alias = "bt-official-prod"
ssh_config_file = "~/.ssh/config"
strict_host_key_checking = true

[projects.official]
repository = "."
include = ["app/**", "config/**", "public/**", "route/**", "extend/**"]
exclude = ["tests/**", "docs/**", "runtime/**", "tmp/**", ".env*"]
protected = [".env", "runtime/**", "app/storage/cert/**", "app/storage/enc/**"]

[projects.official.remotes.dev]
remote_root = "/www/dev/www.bt.cn"
post_commands = ["cd /www/dev/www.bt.cn && php think clear"]
health_urls = ["https://dev.example.com/health"]

[projects.official.remotes.prod]
remote_root = "/www/wwwroot/www.bt.cn"
post_commands = ["cd /www/wwwroot/www.bt.cn && php think clear"]
health_urls = ["https://www.bt.cn/api/oauth/jwks"]
```

When more than one named remote exists, every command requires an explicit
`--remote NAME`. This fail-closed behavior prevents an omitted option from
silently choosing production. You can set top-level `default_remote = "dev"`
when an intentional default is preferable. `remote_root`, `post_commands`, and
`health_urls` inherit their project-level values when an environment does not
override them. Deployment history and rollback backups are isolated by project
and remote.

v0.2 的 target identity、旧历史迁移、bootstrap、policy migration 和 recover
流程见 [状态运维指南](docs/v0.2-state-operations.md)；Host/Docker 构建、
1Password 注入、remote override 和 artifact 信任边界见
[构建产物与秘密安全指南](docs/v0.2-build-artifacts.md)。

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
# Deploy one commit. Its first parent is the expected remote baseline.
git-deploy deploy official --revisions COMMIT --dry-run

# Deploy a continuous range. COMMIT_A is the baseline and is not reapplied.
git-deploy deploy official --revisions COMMIT_A..COMMIT_B --dry-run

# Combine multiple continuous and non-contiguous selections.
git-deploy deploy official --revisions COMMIT_1 COMMIT_3 COMMIT_5..COMMIT_8 --dry-run

# Equivalent dedicated preview command.
git-deploy plan official --revisions COMMIT_A..COMMIT_B

# Read-only remote drift check; still performs no writes.
git-deploy deploy official --revisions COMMIT_A..COMMIT_B \
  --dry-run --check-remote

# Apply a deployment.
git-deploy deploy official --revisions COMMIT_A..COMMIT_B --remote prod --yes

# Deploy the same project to development instead.
git-deploy deploy official --revisions COMMIT_A..COMMIT_B --remote dev --yes

# Build configured artifacts locally without connecting to the remote.
git-deploy build official --revisions COMMIT_B --remote prod

# Inspect and verify the selected physical target state.
git-deploy state inspect official --remote prod
git-deploy state verify official --remote prod

# Show local deployment history and restore the latest successful deployment.
git-deploy history official --remote prod
git-deploy verify official --deployment DEPLOYMENT_ID --remote prod
git-deploy rollback official --latest --remote prod --dry-run
git-deploy rollback official --latest --remote prod --yes
```

`--revisions` uses these rules:

- `COMMIT` selects that commit's change against its first parent.
- `FROM..TO` selects first-parent commits after `FROM` through `TO`.
- Multiple selectors are deduplicated and applied in Git history order, not
  command-line order.
- All selections must belong to one first-parent history. A merge commit is
  interpreted against its first parent.
- The parent of the oldest selected commit is the expected remote baseline.
- If omitted commits make a selected patch impossible to apply cleanly,
  planning fails before any remote connection.

`all` still selects every configured project. The same selectors are resolved
independently in each repository, so symbolic expressions are convenient when
their histories differ:

```bash
git-deploy deploy all --revisions HEAD~1..HEAD --dry-run
```

Run separate commands when projects require different revision selections.
The former `--from`, `--to`, and `--range` options are intentionally removed.

`--deployment` is the local deployment record ID printed after a successful
deployment. A unique prefix is accepted. `--latest` selects the newest record
whose status is still `succeeded`; `rollback all` therefore requires
`--latest` rather than one shared deployment ID.

## Safety model

- Target bytes are read from a real or locally composed Git snapshot, never
  from the working tree.
- Non-contiguous selections are replayed in an isolated temporary Git index
  and object directory; the working tree, current branch, normal Git index,
  and repository object database are not modified.
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
