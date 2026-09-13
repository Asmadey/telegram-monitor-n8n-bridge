#!/usr/bin/env bash
# Защита ветки main (задача 12.9 docs/PLAN.md).
#
# Зачем. Пять джобов CI совещательные, пока GitHub не обязан их спрашивать:
# 2026-09-10 правило «сливать только зелёное» нарушено трижды за одну сессию.
# `scripts/merge-when-green.sh` закрывает дисциплину со стороны агента, но
# скрипт можно забыть вызвать — настройку забыть нельзя.
#
# Что делает: требует прохождения пяти обязательных проверок до слияния в main.
# Обзор (review) НЕ требуется: второго человека в проекте нет, и требование
# ревью просто остановило бы выкладку насовсем.
#
#   scripts/protect-main.sh            # проверки обязательны для всех, включая админов
#   scripts/protect-main.sh --soft     # админы могут слить мимо проверок
#   scripts/protect-main.sh --status   # показать текущее состояние, ничего не менять
#   scripts/protect-main.sh --remove   # снять защиту
#
# --soft оставляет лазейку ровно тому, кто уже ею пользовался: агент и владелец
# — администраторы. Смысл защиты появляется на строгом режиме.

set -euo pipefail

REPO="${REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
BRANCH="${BRANCH:-main}"
CHECKS=(lint secret-scan security test typecheck)

case "${1:-}" in
  --status)
    if ! gh api "repos/$REPO/branches/$BRANCH/protection" 2>/dev/null 1>/tmp/protection.$$; then
      rm -f "/tmp/protection.$$"
      echo "защиты у $REPO@$BRANCH нет вовсе: все проверки совещательные"
      exit 1
    fi
    cat "/tmp/protection.$$"; rm -f "/tmp/protection.$$"
    exit 0
    ;;
  --remove)
    gh api -X DELETE "repos/$REPO/branches/$BRANCH/protection"
    echo "защита снята с $REPO@$BRANCH"
    exit 0
    ;;
  --soft) ENFORCE_ADMINS=false ;;
  "")     ENFORCE_ADMINS=true ;;
  *)      echo "неизвестный ключ: $1" >&2; exit 2 ;;
esac

contexts=$(printf '"%s",' "${CHECKS[@]}")
contexts="[${contexts%,}]"

# strict=false намеренно: strict требует, чтобы ветка была свежее main, то есть
# каждый PR пришлось бы перебазировать вручную ради проверки, которая ничего не
# говорит о качестве кода.
gh api -X PUT "repos/$REPO/branches/$BRANCH/protection" \
  --input - <<JSON > /dev/null
{
  "required_status_checks": { "strict": false, "contexts": $contexts },
  "enforce_admins": $ENFORCE_ADMINS,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON

echo "защита включена для $REPO@$BRANCH"
echo "  обязательные проверки: ${CHECKS[*]}"
echo "  распространяется на администраторов: $ENFORCE_ADMINS"
