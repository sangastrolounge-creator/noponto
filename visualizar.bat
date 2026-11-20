@echo off
rem Abre o No Ponto LOCAL com uma empresa de demonstracao, para ver as mudancas antes de publicar.
rem Usa um banco separado (dados\visualizar.db), recriado a cada vez. Nao toca no dados\turno.db.
chcp 65001 >nul
cd /d "%~dp0"
set LOG=%~dp0visualizar.log
set DATABASE_URL=sqlite:///dados/visualizar.db
set SECRET_KEY=visualizar-local
set SEM_AGENDADOR=1
set IDIOMA_PADRAO=pt
set PUBLIC_URL=http://localhost:8100
set APP_NOME=No Ponto
set ASAAS_API_KEY=
set ASAAS_WEBHOOK_TOKEN=
set ANTHROPIC_API_KEY=
set SMTP_HOST=
set ADMIN_EMAIL=
if not exist dados mkdir dados
if exist dados\visualizar.db del dados\visualizar.db
echo ==== Visualizar %date% %time% ==== > "%LOG%"
echo Criando a empresa de demonstracao...
.venv\Scripts\python tools\demo_completa.py >> "%LOG%" 2>&1
if errorlevel 1 (echo FALHOU A DEMONSTRACAO >> "%LOG%" & echo Falhou: veja visualizar.log & pause & exit /b 1)
echo Sistema local no ar em http://localhost:8100  (feche esta janela para desligar)
echo SERVIDOR LIGADO >> "%LOG%"
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8100 >> "%LOG%" 2>&1
