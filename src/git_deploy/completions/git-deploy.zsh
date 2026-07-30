#compdef git-deploy
# zsh completion for git-deploy
# Enable: eval "$(git-deploy completion zsh)"
# Or place as _git-deploy on fpath (compinit maps via #compdef).

_git_deploy() {
  local -a opts actions completion_kinds
  local targets=""
  local -a target_cmd=(git-deploy)
  local i

  opts=(
    --dry-run
    --remote-plan
    --recover
    --skip-build
    --full
    --yes
    --config
    --workspace
    --verbose
    --create-root
    --no-create-root
    --force
    --probe-ftp-hybrid
    --version
    --help
  )
  actions=(build doctor init bootstrap completion)
  completion_kinds=(bash zsh targets install)

  for ((i = 2; i < CURRENT; i++)); do
    case "${words[i]}" in
      --config|--workspace)
        if (( i + 1 < CURRENT )); then
          target_cmd+=("${words[i]}" "${words[i+1]}")
          ((i++))
        fi
        ;;
    esac
  done

  if (( $+commands[git-deploy] )); then
    targets="${(j: :)${(f)$("${target_cmd[@]}" completion targets 2>/dev/null)}}"
  fi

  local -a positionals=()
  for ((i = 2; i < CURRENT; i++)); do
    case "${words[i]}" in
      --config|--workspace)
        ((i++))
        ;;
      -*)
        ;;
      *)
        positionals+=("${words[i]}")
        ;;
    esac
  done

  if [[ "${words[CURRENT]}" == -* ]]; then
    _describe -t options 'options' opts
    return
  fi

  case "${words[CURRENT-1]}" in
    --config|--workspace)
      _files
      return
      ;;
  esac

  if (( ${#positionals} == 0 )); then
    local -a first
    first=($actions)
    if [[ -n "$targets" ]]; then
      first+=(${=targets})
    fi
    _describe -t commands 'command or target' first
    return
  fi

  if (( ${#positionals} == 1 )); then
    case "${positionals[1]}" in
      completion)
        _describe -t shells 'completion kind' completion_kinds
        ;;
      doctor|bootstrap|build|*)
        if [[ -n "$targets" ]]; then
          local -a t
          t=(${=targets})
          _describe -t targets 'target' t
        fi
        ;;
    esac
  fi
}

# Safe for both `eval "$(git-deploy completion zsh)"` and fpath-loaded scripts.
if (( $+functions[compdef] )); then
  compdef _git_deploy git-deploy
fi
