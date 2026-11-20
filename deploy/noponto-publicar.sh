#!/bin/bash
# Publica o No Ponto no VPS. Recebe pela entrada padrão um .tar.gz com app/, requirements.txt e Dockerfile.
# É o único comando que a chave do GitHub pode rodar (command= no authorized_keys).
# Passos: backup do banco e do código -> troca o código -> reconstrói -> confere -> se falhar, volta.
set -euo pipefail
PASTA=/home/turno-app
BACKUPS=/root/noponto-backups
MANTER=20
STAMP=$(date +%Y%m%d-%H%M%S)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
exec 9>/var/lock/noponto-publicar.lock
flock -n 9 || { echo "Outra publicação em andamento"; exit 1; }

cd "$PASTA"
mkdir -p "$BACKUPS"

echo "== recebendo o pacote"
head -c 50000000 > "$TMP/pacote.tar.gz"
tar tzf "$TMP/pacote.tar.gz" > "$TMP/lista"
grep -qx 'app/main.py' "$TMP/lista" && grep -qx 'Dockerfile' "$TMP/lista" && grep -qx 'requirements.txt' "$TMP/lista" \
  || { echo "Pacote inválido"; exit 1; }
if grep -Eq '(^/|(^|/)\.\.(/|$))' "$TMP/lista"; then echo "Pacote com caminho inválido"; exit 1; fi
if grep -Eq '(^|/)(\.env|.*\.db)$' "$TMP/lista"; then echo "Pacote com .env ou banco: recusado"; exit 1; fi

echo "== backup do banco"
docker compose exec -T app python -c "
import sqlite3
s = sqlite3.connect('/dados/turno.db'); d = sqlite3.connect('/dados/backup-publicacao.db')
s.backup(d); d.close()
print('usuarios:', s.execute('select count(*) from usuarios').fetchone()[0])"
docker compose cp app:/dados/backup-publicacao.db "$BACKUPS/banco-$STAMP.db"
tar czf "$BACKUPS/codigo-$STAMP.tgz" app requirements.txt Dockerfile
echo "backup: $BACKUPS/banco-$STAMP.db"

echo "== trocando o código"
mkdir "$TMP/novo"
tar xzf "$TMP/pacote.tar.gz" -C "$TMP/novo" app requirements.txt Dockerfile
rm -rf app.anterior
mv app app.anterior
mv "$TMP/novo/app" app
cp "$TMP/novo/requirements.txt" "$TMP/novo/Dockerfile" .

no_ar() {
  for _ in $(seq 1 40); do
    a=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8096/ || true)
    b=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8096/entrar || true)
    if [ "$a" = 200 ] && [ "$b" = 200 ]; then return 0; fi
    sleep 3
  done
  echo "respostas: / $a, /entrar $b"
  return 1
}

voltar() {
  echo "!! a versão nova não respondeu: voltando para a anterior"
  docker compose logs --tail 40 app || true
  rm -rf app
  tar xzf "$BACKUPS/codigo-$STAMP.tgz"
  docker compose up -d --build 2>&1 | tail -n 5
  no_ar && echo "versão anterior de volta no ar" || echo "!! a versão anterior também não respondeu"
  exit 1
}

echo "== reconstruindo"
docker compose up -d --build 2>&1 | tail -n 8 || voltar

echo "== conferindo"
no_ar || voltar
if docker compose logs --since 2m app 2>&1 | grep -q 'OperationalError'; then
  echo "!! erro de banco no log"; voltar
fi
rm -rf app.anterior

ls -1t "$BACKUPS"/banco-*.db 2>/dev/null | tail -n +$((MANTER + 1)) | xargs -r rm -f
ls -1t "$BACKUPS"/codigo-*.tgz 2>/dev/null | tail -n +$((MANTER + 1)) | xargs -r rm -f
echo "== publicado ($STAMP)"
