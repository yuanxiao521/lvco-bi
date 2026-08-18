$ErrorActionPreference = "Stop"
$LogFile = "e:\BI\LvcoBI\lvco-bi\backend\full_output.log"
& "uvicorn" "app.main:app" "--host" "0.0.0.0" "--port" "8000" *>&1 | Tee-Object -FilePath $LogFile
