$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BuildDeps = Join-Path $ProjectRoot ".build-deps"
$BuildOutput = Join-Path $ProjectRoot "dist"

$BuildPython = $env:SIGNUP_BUILD_PYTHON
if (-not $BuildPython) {
    $SystemPython = Get-Command python -ErrorAction SilentlyContinue
    if (-not $SystemPython) {
        throw "未找到 Python。请安装 64 位 Python 3.10 或更新版本，并确保命令行可运行 python。"
    }
    $BuildPython = $SystemPython.Source
}
if (-not (Test-Path $BuildPython)) { throw "构建用 Python 不存在：$BuildPython" }

if (-not (Test-Path (Join-Path $BuildDeps "PyInstaller\__init__.py")) -or
    -not (Test-Path (Join-Path $BuildDeps "rapidocr\__init__.py")) -or
    -not (Test-Path (Join-Path $BuildDeps "onnxruntime\__init__.py")) -or
    -not (Test-Path (Join-Path $BuildDeps "cv2\__init__.py"))) {
    & $BuildPython -m pip install --upgrade --target $BuildDeps -r (Join-Path $ProjectRoot "requirements.txt") pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "Python 依赖安装失败。" }
}

# Ensure OCR models are available in the build environment and packaged with RapidOCR.
$env:PYTHONPATH = "$BuildDeps$([System.IO.Path]::PathSeparator)$env:PYTHONPATH"
$RapidOcrCli = Join-Path $BuildDeps "bin\rapidocr.exe"
if (-not (Test-Path (Join-Path $BuildDeps "rapidocr\models"))) {
    & $RapidOcrCli download_models
    if ($LASTEXITCODE -ne 0) { throw "RapidOCR 模型准备失败。" }
}

Push-Location $ProjectRoot
try {
    & $BuildPython -m PyInstaller --noconfirm --clean --windowed `
        --name "SignupDesk" `
        --add-data "assets;assets" `
        --collect-all rapidocr `
        --collect-all onnxruntime `
        --collect-all cv2 `
        --collect-all uiautomation `
        app.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败。" }
}
finally {
    Pop-Location
}

$InstallerExe = Join-Path $BuildOutput "SignupDeskSetup.exe"
Push-Location $ProjectRoot
try {
    & $BuildPython -m PyInstaller --noconfirm --clean --onefile --windowed `
        --name "SignupDeskSetup" `
        --add-data "dist/SignupDesk;payload" `
        packaging\installer_bootstrap.py
    if ($LASTEXITCODE -ne 0) { throw "安装程序构建失败。" }
}
finally {
    Pop-Location
}

if (-not (Test-Path $InstallerExe)) { throw "安装包未生成：$InstallerExe" }
Write-Host "安装包已生成：$InstallerExe"
