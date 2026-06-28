@echo off
rem team-brain ACP tap wrapper for Devin's `devin acp` agent (Windows).
rem Set this .cmd as your IDE's ACP agent command in place of the bundled
rem devin.exe. Forwards every byte verbatim and records user<->LLM activity.
rem The IDE's own args (e.g. `acp`) are passed through via %*.
rem
rem   env knobs: TEAMBRAIN_NS          namespace (default team-eng)
rem              TEAMBRAIN_ACP_RECORD  raw frame log (default %USERPROFILE%\devin-acp.jsonl)
rem              DEVIN_BIN             real agent path (auto-detected if unset)
rem              TEAMBRAIN_PYTHON      python interpreter (default python)
setlocal
set "HERE=%~dp0.."
set "PYTHONPATH=%HERE%;%PYTHONPATH%"
if "%TEAMBRAIN_NS%"=="" set "TEAMBRAIN_NS=team-eng"
if "%TEAMBRAIN_ACP_RECORD%"=="" set "TEAMBRAIN_ACP_RECORD=%USERPROFILE%\devin-acp.jsonl"
if "%TEAMBRAIN_PYTHON%"=="" set "TEAMBRAIN_PYTHON=python"
"%TEAMBRAIN_PYTHON%" -m teambrain.connectors.acp_tap --namespace "%TEAMBRAIN_NS%" --record "%TEAMBRAIN_ACP_RECORD%" --devin-auto -- %*
endlocal
