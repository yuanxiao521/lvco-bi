$ErrorActionPreference = "Stop"
# 锁定工作目录为脚本所在目录（backend/）：
# 否则若从仓库根目录执行，Python 会把 `app` 解析到 site-packages 里废弃的 Flask 包
# （报 app.module_one / config 之类的 ModuleNotFoundError），而不是本项目 backend/app。
Set-Location $PSScriptRoot
$LogFile = "e:\BI\LvcoBI\lvco-bi\backend\full_output.log"
& "uvicorn" "app.main:app" "--host" "0.0.0.0" "--port" "8000" *>&1 | Tee-Object -FilePath $LogFile
