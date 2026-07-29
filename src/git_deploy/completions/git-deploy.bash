# bash completion for git-deploy
# Enable: eval "$(git-deploy completion bash)"
# Or: source this file after git-deploy is on PATH.

_git_deploy() {
  local cur prev
  COMPREPLY=()
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"

  local opts="--dry-run --remote-plan --recover --skip-build --full --yes --config --workspace --verbose --create-root --no-create-root --force --probe-ftp-hybrid --version --help"
  local actions="build doctor init bootstrap completion"
  local completion_kinds="bash zsh targets install"

  case "$prev" in
    --config|--workspace)
      COMPREPLY=( $(compgen -f -- "$cur") )
      return 0
      ;;
  esac

  if [[ "$cur" == -* ]]; then
    COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
    return 0
  fi

  # Find first non-option positionals after the command name.
  local -a positionals=()
  local -a target_cmd=(git-deploy)
  local i word
  for ((i = 1; i < COMP_CWORD; i++)); do
    word="${COMP_WORDS[i]}"
    case "$word" in
      --config|--workspace)
        if [[ -n "${COMP_WORDS[i+1]:-}" ]]; then
          target_cmd+=("$word" "${COMP_WORDS[i+1]}")
          ((i++))
        fi
        ;;
      -*)
        ;;
      *)
        positionals+=("$word")
        ;;
    esac
  done

  local targets=""
  if command -v git-deploy >/dev/null 2>&1; then
    targets=$("${target_cmd[@]}" completion targets 2>/dev/null | tr '\n' ' ')
  fi

  if ((${#positionals[@]} == 0)); then
    COMPREPLY=( $(compgen -W "$actions $targets" -- "$cur") )
    return 0
  fi

  if ((${#positionals[@]} == 1)); then
    case "${positionals[0]}" in
      completion)
        COMPREPLY=( $(compgen -W "$completion_kinds" -- "$cur") )
        ;;
      doctor|bootstrap|build)
        COMPREPLY=( $(compgen -W "$targets" -- "$cur") )
        ;;
      *)
        COMPREPLY=( $(compgen -W "$targets" -- "$cur") )
        ;;
    esac
    return 0
  fi

  return 0
}

complete -F _git_deploy git-deploy
