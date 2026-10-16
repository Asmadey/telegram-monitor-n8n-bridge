#!/usr/bin/env bash
# Слияние PR только после того, как ВСЕ обязательные джобы дали «успех».
#
# Зачем скрипт, а не команда в документе. Правило «дождись CI» за одну сессию
# нарушено трижды, каждый раз по-новому:
#   1. слияние вызвали сразу после ожидания, не прочитав исход (PR #45, красный);
#   2. слияние вызвали, когда проверки ЕЩЁ НЕ СТАРТОВАЛИ: список состоял из
#      одних Vercel-строк, «провалов нет» было правдой и бессмыслицей (PR #47);
#   3. `gh pr checks --watch` вернулся мгновенно по той же причине — он ждёт
#      те проверки, что уже зарегистрированы, а джобы Actions появляются позже
#      (PR #48).
# Общее у всех трёх: проверялось ОТСУТСТВИЕ провала вместо НАЛИЧИЯ успеха.
# Поэтому здесь список обязательных джобов задан явно и ждётся каждый.
#
# Использование:  scripts/merge-when-green.sh <номер PR> [--no-merge]
set -euo pipefail

PR="${1:?укажите номер PR}"
NO_MERGE="${2:-}"
REQUIRED=(lint secret-scan security test typecheck)
DEADLINE=$(( SECONDS + 1800 ))

# Проверки читаются через `gh api`, а НЕ через `gh pr checks`. Причина не
# в стиле: `gh pr checks` есть только в настоящем GitHub CLI, а в облачных
# сессиях Claude Code вместо него встроенный клиент, у которого из подкоманд
# работает один `gh api`. Скрипт на `gh pr checks` там падал с «no such
# command», то есть обязательный путь выкладки был недоступен в половине
# сред. `gh api` есть в обеих, поэтому путь один — расходиться нечему.
SLUG="$(git remote get-url origin | sed -E 's#^.*github\.com[:/]##; s#\.git$##')"
HEAD_SHA="$(gh api "repos/$SLUG/pulls/$PR" --jq .head.sha)"
[[ -n "$SLUG" && -n "$HEAD_SHA" ]] || { echo "не определить repo/head для PR $PR" >&2; exit 3; }

status_of() {  # имя джоба → pass|fail|pending|absent
  # Берётся НАЛИЧИЕ conclusion=success. Незавершённый джоб и джоб, которого
  # ещё нет в списке, одинаково «не успех» — ровно та ошибка, из-за которой
  # этот скрипт и появился.
  gh api "repos/$SLUG/commits/$HEAD_SHA/check-runs" --paginate \
      --jq '.check_runs[] | "\(.name)\t\(.status)\t\(.conclusion // "")"' 2>/dev/null \
    | awk -v job="$1" -F'\t' '
        $1 == job {
          found = 1
          if ($3 == "success") { print "pass"; exit }
          if ($2 == "completed") { print "fail"; exit }
          print "pending"; exit
        }
        END { if (!found) print "absent" }'
}

while :; do
  pending=() failed=()
  for job in "${REQUIRED[@]}"; do
    case "$(status_of "$job")" in
      pass) ;;
      fail) failed+=("$job") ;;
      *)    pending+=("$job") ;;   # pending и absent ждём одинаково
    esac
  done

  if (( ${#failed[@]} )); then
    echo "ПРОВАЛ: ${failed[*]} — слияния не будет" >&2
    exit 1
  fi
  if (( ${#pending[@]} == 0 )); then
    echo "все обязательные джобы зелёные: ${REQUIRED[*]}"
    break
  fi
  if (( SECONDS > DEADLINE )); then
    echo "истекло ожидание, так и не отчитались: ${pending[*]}" >&2
    exit 2
  fi
  echo "  жду: ${pending[*]}"
  sleep 20
done

if [[ "$NO_MERGE" == "--no-merge" ]]; then
  echo "(--no-merge: слияние не выполняется)"
  exit 0
fi
gh api -X PUT "repos/$SLUG/pulls/$PR/merge" -f merge_method=merge
