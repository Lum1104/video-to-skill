@echo off
setlocal
if defined VIDEO_TO_SKILL_PYTHON (
  "%VIDEO_TO_SKILL_PYTHON%" "%~dp0video_to_skill.py" %*
) else (
  py -3.12 -c "import sys" >nul 2>&1
  if not errorlevel 1 (
    py -3.12 "%~dp0video_to_skill.py" %*
  ) else (
    py -3.11 -c "import sys" >nul 2>&1
    if not errorlevel 1 (
      py -3.11 "%~dp0video_to_skill.py" %*
    ) else (
      py -3.13 -c "import sys" >nul 2>&1
      if not errorlevel 1 (
        py -3.13 "%~dp0video_to_skill.py" %*
      ) else (
        python "%~dp0video_to_skill.py" %*
      )
    )
  )
)
