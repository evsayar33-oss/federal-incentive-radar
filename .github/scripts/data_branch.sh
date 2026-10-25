#!/usr/bin/env bash
# 'data' dalını ./store klasörüne bağlar (setup) ve tek commit'lik anlık görüntü olarak yayımlar (publish).
# Dal her zaman TEK commit tutar: depo zamanla şişmez. Tabloların kendi geçmişi dosyaların içindedir.
# Streamlit etiketleri aynı dala yazabilir; çakışmada etiket dosyası korunur (force-with-lease).
set -euo pipefail
CMD="${1:-}"
BRANCH="${DATA_BRANCH:-data}"
git config --global user.name "radar-bot"
git config --global user.email "radar-bot@users.noreply.github.com"

setup() {
  if git fetch --depth=1 origin "$BRANCH" 2>/dev/null; then
    git worktree add -f store FETCH_HEAD --detach
    git -C store rev-parse HEAD > .data_base_sha
  else
    echo "'$BRANCH' dalı yok, yeni oluşturulacak."
    git worktree add -f --detach store
    (cd store && git rm -rq . 2>/dev/null || true && find . -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +)
    : > .data_base_sha
  fi
}

snapshot() {
  cd store
  git checkout -q --orphan "snap-$$" 2>/dev/null || git checkout -q -B "snap-$$"
  git add -A
  git commit -q -m "data snapshot ${RUN_LABEL:-run} $(date -u +%Y-%m-%dT%H:%MZ)" || true
  cd ..
}

publish() {
  local base; base="$(cat .data_base_sha 2>/dev/null || true)"
  snapshot
  for i in 1 2 3 4; do
    if git -C store push --force-with-lease="$BRANCH:$base" origin "HEAD:refs/heads/$BRANCH"; then
      echo "Yayımlandı."; return 0
    fi
    echo "Dal değişmiş (muhtemelen yeni etiket). Etiketler birleştirilip tekrar deneniyor ($i)..."
    git -C store fetch --depth=1 origin "$BRANCH"
    base="$(git -C store rev-parse FETCH_HEAD)"
    mkdir -p store/labels
    git -C store show FETCH_HEAD:labels/labels.jsonl > store/labels/labels.jsonl 2>/dev/null || true
    (cd store && git add -A && git commit -q --amend -m "data snapshot ${RUN_LABEL:-run} (merged labels)")
    sleep $((i * 3))
  done
  echo "Yayımlama başarısız."; return 1
}

case "$CMD" in
  setup) setup ;;
  publish) publish ;;
  *) echo "kullanım: $0 setup|publish"; exit 2 ;;
esac
