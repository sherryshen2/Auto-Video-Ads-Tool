"""Build the small ADB helper using the locally installed Android SDK and JDK."""
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / '.runtime' / 'video-buttons.jar'


def build(sdk: Path) -> Path:
    sources = sorted((ROOT / 'android').glob('*.java'))
    if OUTPUT.exists() and OUTPUT.stat().st_mtime >= max(*(source.stat().st_mtime for source in sources), Path(__file__).stat().st_mtime):
        return OUTPUT
    javac = shutil.which('javac')
    studio_javac = Path('/Applications/Android Studio.app/Contents/jbr/Contents/Home/bin/javac')
    if studio_javac.is_file():
        javac = str(studio_javac)
    platforms = sorted(sdk.glob('platforms/android-*/android.jar'), reverse=True)
    build_tools = sorted(sdk.glob('build-tools/*/d8'), reverse=True)
    if not javac or not platforms or not build_tools:
        raise RuntimeError('需要本机 Android SDK、Build Tools 和 Java 编译器。')
    classes = OUTPUT.parent / 'classes'
    classes.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment['JAVA_HOME'] = str(Path(javac).resolve().parent.parent)
    try:
        subprocess.run([javac, '--release', '8', '-cp', str(platforms[0]), '-d', str(classes), *map(str, sources)],
                       check=True, capture_output=True, text=True, timeout=40, env=environment)
        subprocess.run([str(build_tools[0]), '--min-api', '33', '--lib', str(platforms[0]),
                        '--output', str(OUTPUT), *map(str, classes.glob('*.class'))],
                       check=True, capture_output=True, text=True, timeout=40, env=environment)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f'按钮识别程序编译失败：{(error.stderr or str(error))[-500:]}') from error
    return OUTPUT


def build_clipboard(sdk: Path) -> Path:
    """Build our foreground clipboard bridge; no network or clipboard background permission."""
    destination = ROOT / '.runtime' / 'clipboard'
    artifact = destination / 'clipboard.apk'
    source = ROOT / 'android' / 'clipboard' / 'ClipboardActivity.java'
    manifest = source.with_name('AndroidManifest.xml')
    if artifact.exists() and artifact.stat().st_mtime >= max(
            source.stat().st_mtime, manifest.stat().st_mtime, Path(__file__).stat().st_mtime):
        return artifact
    java = Path('/Applications/Android Studio.app/Contents/jbr/Contents/Home')
    if not (java / 'bin/javac').exists():
        executable = shutil.which('javac')
        if not executable:
            raise RuntimeError('找不到 Java 编译器。')
        java = Path(executable).resolve().parent.parent
    platforms = sorted(sdk.glob('platforms/android-*/android.jar'), reverse=True)
    tool_dirs = sorted(sdk.glob('build-tools/*/aapt2'), reverse=True)
    if not platforms or not tool_dirs:
        raise RuntimeError('剪贴板读取需要 Android SDK 和 Build Tools。')
    platform, tools = platforms[0], tool_dirs[0].parent
    classes = destination / 'classes'
    dex = destination / 'dex'
    classes.mkdir(parents=True, exist_ok=True)
    dex.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, JAVA_HOME=str(java))

    def run(args):
        try:
            subprocess.run(list(map(str, args)), check=True, capture_output=True, text=True,
                           timeout=60, env=environment)
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f'剪贴板辅助程序编译失败：{(error.stderr or error.stdout or str(error))[-700:]}') from error

    run([java / 'bin/javac', '--release', '8', '-cp', platform, '-d', classes, source])
    run([tools / 'd8', '--min-api', '33', '--lib', platform, '--output', dex,
         *classes.rglob('*.class')])
    unsigned = destination / 'unsigned.apk'
    run([tools / 'aapt2', 'link', '-o', unsigned, '-I', platform, '--manifest', manifest])
    with zipfile.ZipFile(unsigned, 'a') as package:
        package.write(dex / 'classes.dex', 'classes.dex')
    aligned = destination / 'aligned.apk'
    run([tools / 'zipalign', '-f', '4', unsigned, aligned])
    key = destination / 'local-helper.p12'
    if not key.exists():
        run([java / 'bin/keytool', '-genkeypair', '-keystore', key, '-storepass', 'local-videoqa',
             '-keypass', 'local-videoqa', '-alias', 'videoqa', '-keyalg', 'RSA', '-keysize', '2048',
             '-validity', '3650', '-dname', 'CN=Local Video QA Helper', '-noprompt'])
    run([tools / 'apksigner', 'sign', '--ks', key, '--ks-pass', 'pass:local-videoqa',
         '--out', artifact, aligned])
    return artifact


if __name__ == '__main__':
    sdk = Path(os.environ.get('ANDROID_SDK_ROOT') or os.environ.get('ANDROID_HOME') or
               str(Path.home() / 'Library/Android/sdk'))
    print(build(sdk))
