import json
import hashlib
import html
import io
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, session, send_file, send_from_directory

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
GAMES_DIR = BASE_DIR / "GeneratedGames"

STATIC_DIR.mkdir(parents=True, exist_ok=True)
GAMES_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=None)
app.secret_key = os.getenv("SESSION_SECRET") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("COOKIE_SECURE", "false").lower() == "true"

ROLE_PASSWORDS = {
    "admin": os.getenv("ADMIN_PASSWORD", ""),
    "normal": os.getenv("NORMAL_PASSWORD", ""),
    "download": os.getenv("DOWNLOAD_PASSWORD", ""),
}
API_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = "gemini-3.5-flash-lite"

# The selected genre is a hard gameplay requirement, not just a visual theme.
GENRE_RULES = {
    "Action": "Build a real-time action loop with responsive movement, clear threats, useful feedback, and a definite win or lose condition.",
    "Simulation": "Let the player operate or manage a system; show changing values and make each action produce understandable feedback.",
    "Puzzle": "Make solving a puzzle the main activity. Follow the idea's puzzle rules, such as matching, logic, sorting, or spatial challenges.",
    "Adventure": "Center exploration and discovery; give the player a small setting, a goal, and at least one quest-like task.",
    "Racing": "Center vehicle control and racing; include a course, steering, speed, and a finish line, timer, or rival.",
    "Role Playing": "Give the player a character, a small quest, meaningful choices, and visible progress such as stats, items, or upgrades.",
    "Strategy": "Make planning the key skill; use turns or tactical choices and meaningful use of units or resources.",
    "Sports": "Represent the selected sport with understandable rules, direct player control, and a visible score or match result.",
    "Card": "Make a card hand and card rules central; let the player select and play cards and explain how to win.",
    "Board": "Use a visible board or grid, clear turns, legal moves, and a clear way to win or finish.",
    "Educational": "Teach or practice the requested subject through interactive questions or activities, with answers and helpful feedback.",
    "Word": "Make spelling, finding, unscrambling, or forming words the main activity; validate answers and show helpful feedback.",
    "Platformer": "Make running and jumping across platforms central; include gravity, responsive jumps, safe landings, hazards, and a goal.",
}


def _version_key(value):
    """Sort Android SDK version folder names numerically."""
    return tuple(int(part) for part in re.findall(r"\d+", value)) or (0,)


def _android_tool_paths():
    """Find Android SDK tools and the Java bundled with Android Studio."""
    local_app_data = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData/Local"))
    sdk_candidates = [
        os.getenv("ANDROID_SDK_ROOT"),
        os.getenv("ANDROID_HOME"),
        str(local_app_data / "Android" / "Sdk"),
    ]
    sdk = next(
        (Path(item) for item in sdk_candidates if item and (Path(item) / "platforms").is_dir()),
        None,
    )
    if sdk is None:
        raise RuntimeError(
            "Android SDK not found. In Android Studio, open SDK Manager and install an Android SDK Platform and Android SDK Build-Tools."
        )

    platforms = sorted(
        (item for item in (sdk / "platforms").glob("android-*") if (item / "android.jar").is_file()),
        key=lambda item: _version_key(item.name),
        reverse=True,
    )
    build_tools = sorted(
        (
            item for item in (sdk / "build-tools").iterdir()
            if (item / "aapt.exe").is_file()
            and (item / "zipalign.exe").is_file()
            and (item / "lib" / "d8.jar").is_file()
            and (item / "lib" / "apksigner.jar").is_file()
        ),
        key=lambda item: _version_key(item.name),
        reverse=True,
    ) if (sdk / "build-tools").is_dir() else []
    if not platforms or not build_tools:
        raise RuntimeError(
            "Android SDK is incomplete. Install an Android SDK Platform and Android SDK Build-Tools in Android Studio > SDK Manager."
        )

    java_candidates = [
        Path(os.getenv("JAVA_HOME", "")) / "bin" if os.getenv("JAVA_HOME") else None,
        Path(os.getenv("ANDROID_STUDIO_JBR", "")) / "bin" if os.getenv("ANDROID_STUDIO_JBR") else None,
        Path(os.getenv("ProgramFiles", "C:/Program Files")) / "Android/Android Studio/jbr/bin",
        local_app_data / "Programs/Android Studio/jbr/bin",
    ]
    java_bin = next(
        (
            item for item in java_candidates
            if item and all((item / tool).is_file() for tool in ("java.exe", "javac.exe", "keytool.exe"))
        ),
        None,
    )
    if java_bin is None:
        java = shutil.which("java")
        if java and all((Path(java).parent / tool).is_file() for tool in ("javac.exe", "keytool.exe")):
            java_bin = Path(java).parent
    if java_bin is None:
        raise RuntimeError(
            "A Java JDK was not found. Install Android Studio or set JAVA_HOME to its bundled jbr folder."
        )

    tools = build_tools[0]
    d8_jar = tools / "lib" / "d8.jar"
    signer_jar = tools / "lib" / "apksigner.jar"

    return {
        "sdk": sdk,
        "android_jar": platforms[0] / "android.jar",
        "build_tools": tools,
        "java": java_bin / "java.exe",
        "javac": java_bin / "javac.exe",
        "keytool": java_bin / "keytool.exe",
        "d8_jar": d8_jar,
        "signer_jar": signer_jar,
    }


def _run_build_command(args, cwd):
    """Run one Android build command and include useful diagnostics on failure."""
    result = subprocess.run(
        [str(item) for item in args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode:
        details = (result.stderr or result.stdout or "No diagnostic output.").strip()
        raise RuntimeError(details[-2500:])
    return result.stdout.strip()


def build_android_apk(game_dir, game_name):
    """Wrap this game's single HTML file in a signed Android WebView APK."""
    tools = _android_tool_paths()
    game_dir = Path(game_dir)
    source_html = game_dir / "index.html"
    if not source_html.is_file():
        raise RuntimeError("The saved game index.html is missing.")

    package_token = hashlib.sha256(game_dir.name.encode("utf-8")).hexdigest()[:10]
    package_name = f"com.universalaigamecreator.games.g{package_token}"
    safe_title = re.sub(r"\s+", " ", str(game_name)).strip()[:80] or "AI Game"
    app_label = html.escape(safe_title, quote=True)
    background = "#%06x" % (
        int(hashlib.sha256(safe_title.encode("utf-8")).hexdigest()[:6], 16) & 0xFFFFFF
    )

    manifest = f'''<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="{package_name}" android:versionCode="1" android:versionName="1.0">
  <uses-sdk android:minSdkVersion="23" android:targetSdkVersion="35" />
  <application android:label="{app_label}" android:icon="@drawable/ic_launcher" android:theme="@android:style/Theme.Material.Light.NoActionBar" android:allowBackup="false" android:hardwareAccelerated="true">
    <activity android:name="com.universalaigamecreator.wrapper.MainActivity" android:exported="true" android:screenOrientation="sensor" android:configChanges="orientation|screenSize|keyboardHidden">
      <intent-filter>
        <action android:name="android.intent.action.MAIN" />
        <category android:name="android.intent.category.LAUNCHER" />
      </intent-filter>
    </activity>
  </application>
</manifest>'''
    # Each app gets a small custom-color launcher icon. The actual game logo and
    # lobby are created in its bundled HTML by the game's generation prompt.
    icon = f'''<?xml version="1.0" encoding="utf-8"?>
<vector xmlns:android="http://schemas.android.com/apk/res/android" android:width="48dp" android:height="48dp" android:viewportWidth="48" android:viewportHeight="48">
  <path android:fillColor="{background}" android:pathData="M8,2h32a6,6 0,0 1,6 6v32a6,6 0,0 1,-6 6h-32a6,6 0,0 1,-6 -6v-32a6,6 0,0 1,6 -6z" />
  <path android:fillColor="#FFFFFF" android:pathData="M15,16h18a6,6 0,0 1,5.7 4.1l3,9a4,4 0,0 1,-6.1 4.5l-5,-3.1h-13.2l-5,3.1a4,4 0,0 1,-6.1 -4.5l3,-9a6,6 0,0 1,5.7 -4.1zM17,21v8M13,25h8" />
  <path android:fillColor="#FFFFFF" android:pathData="M32,22a2,2 0,1 0,0.01 0M36,27a2,2 0,1 0,0.01 0" />
</vector>'''
    java_source = '''package com.universalaigamecreator.wrapper;

import android.app.Activity;
import android.os.Bundle;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

public final class MainActivity extends Activity {
    private WebView webView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().setFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN,
                WindowManager.LayoutParams.FLAG_FULLSCREEN);
        hideSystemBars();
        webView = new WebView(this);
        webView.setBackgroundColor(0xFF10121A);
        webView.setWebViewClient(new WebViewClient());
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(true);
        settings.setAllowContentAccess(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        setContentView(webView);
        webView.loadUrl("file:///android_asset/game/index.html");
    }

    private void hideSystemBars() {
        Window window = getWindow();
        window.getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                | View.SYSTEM_UI_FLAG_FULLSCREEN
                | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) hideSystemBars();
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) webView.goBack();
        else super.onBackPressed();
    }
}
'''

    keystore = GAMES_DIR / ".android-debug.keystore"
    if not keystore.exists():
        _run_build_command(
            [tools["keytool"], "-genkeypair", "-v", "-keystore", keystore,
             "-storepass", "android", "-alias", "androiddebugkey", "-keypass", "android",
             "-dname", "CN=Android Debug,O=Universal AI Game Creator,C=US",
             "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000"],
            GAMES_DIR,
        )

    with tempfile.TemporaryDirectory(prefix=".apk-build-", dir=str(game_dir)) as temp_name:
        temp = Path(temp_name)
        (temp / "res" / "drawable").mkdir(parents=True)
        (temp / "src" / "com" / "universalaigamecreator" / "wrapper").mkdir(parents=True)
        (temp / "classes").mkdir()
        (temp / "dex").mkdir()
        (temp / "assets" / "game").mkdir(parents=True)
        (temp / "AndroidManifest.xml").write_text(manifest, encoding="utf-8")
        (temp / "res" / "drawable" / "ic_launcher.xml").write_text(icon, encoding="utf-8")
        java_file = temp / "src" / "com" / "universalaigamecreator" / "wrapper" / "MainActivity.java"
        java_file.write_text(java_source, encoding="utf-8")
        shutil.copyfile(source_html, temp / "assets" / "game" / "index.html")

        unsigned = temp / "unsigned.apk"
        _run_build_command(
            [tools["build_tools"] / "aapt.exe", "package", "-f", "-M",
             temp / "AndroidManifest.xml", "-S", temp / "res", "-I", tools["android_jar"],
             "-F", unsigned],
            temp,
        )
        _run_build_command(
            [tools["javac"], "-source", "8", "-target", "8", "-classpath",
             tools["android_jar"], "-d", temp / "classes", java_file],
            temp,
        )
        classes_jar = temp / "classes.jar"
        with zipfile.ZipFile(classes_jar, "w", zipfile.ZIP_DEFLATED) as jar:
            for class_file in (temp / "classes").rglob("*.class"):
                jar.write(class_file, class_file.relative_to(temp / "classes").as_posix())
        _run_build_command(
            [tools["java"], "-cp", tools["d8_jar"], "com.android.tools.r8.D8",
             "--min-api", "23", "--lib", tools["android_jar"], "--output", temp / "dex", classes_jar],
            temp,
        )
        with zipfile.ZipFile(unsigned, "a", zipfile.ZIP_DEFLATED) as apk:
            apk.write(temp / "dex" / "classes.dex", "classes.dex")
            apk.write(temp / "assets" / "game" / "index.html", "assets/game/index.html")

        aligned = temp / "aligned.apk"
        _run_build_command(
            [tools["build_tools"] / "zipalign.exe", "-f", "4", unsigned, aligned],
            temp,
        )
        signed = temp / "signed.apk"
        _run_build_command(
            [tools["java"], "-jar", tools["signer_jar"], "sign", "--ks", keystore,
             "--ks-key-alias", "androiddebugkey", "--ks-pass", "pass:android",
             "--key-pass", "pass:android", "--out", signed, aligned],
            temp,
        )
        _run_build_command(
            [tools["java"], "-jar", tools["signer_jar"], "verify", "--verbose", signed],
            temp,
        )
        final_apk = game_dir / "android.apk"
        shutil.copyfile(signed, final_apk)
        return final_apk


def logged_in():
    return session.get("role") in ROLE_PASSWORDS


def role_can_create(role=None):
    return (role or session.get("role")) in {"admin", "normal"}


def role_can_download(role=None):
    return (role or session.get("role")) in {"admin", "download"}


def safe_game_folder(folder):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", folder or ""):
        return None

    root = (GAMES_DIR / folder).resolve()
    if root.parent != GAMES_DIR.resolve() or not root.is_dir():
        return None
    return root


@app.get("/")
def home():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/api/session")
def check_session():
    return jsonify(logged_in=logged_in(), role=session.get("role"))


@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    password = str(data.get("password", ""))

    if not any(ROLE_PASSWORDS.values()):
        return jsonify(error="Set the three role passwords in your environment first."), 500

    for role, saved_password in ROLE_PASSWORDS.items():
        if saved_password and secrets.compare_digest(password, saved_password):
            session.clear()
            session["role"] = role
            return jsonify(ok=True, role=role)

    return jsonify(error="Wrong password."), 401


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/games")
def list_games():
    if not logged_in():
        return jsonify(error="Please log in first."), 401

    games = []
    try:
        for folder in GAMES_DIR.iterdir():
            if not folder.is_dir():
                continue

            safe_folder = safe_game_folder(folder.name)
            if safe_folder is None:
                continue

            game_file = safe_folder / "index.html"
            if not game_file.is_file():
                continue

            try:
                modified = game_file.stat().st_mtime
                html = game_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            match = re.search(
                r"<title[^>]*>(.*?)</title>",
                html,
                flags=re.IGNORECASE | re.DOTALL,
            )
            title = (
                re.sub(r"\s+", " ", match.group(1)).strip()
                if match
                else folder.name
            )
            title = re.sub(r"<[^>]*>", "", title)[:100]
            title = title or folder.name

            games.append(
                {
                    "folder": folder.name,
                    "name": title,
                    "modified": modified,
                    "play_url": (
                        f"/games/{folder.name}/index.html"
                        if role_can_create() else None
                    ),
                    "download_url": f"/games/{folder.name}/download",
                    "apk_url": (
                        f"/games/{folder.name}/android.apk"
                        if (safe_folder / "android.apk").is_file()
                        else None
                    ),
                }
            )

        games.sort(key=lambda game: game["modified"], reverse=True)
        return jsonify(games=games)

    except OSError:
        app.logger.exception("Could not read game library")
        return jsonify(error="Could not load the game library."), 500


def parse_gemini_json(result):
    """Parse Gemini JSON and repair common bad escapes inside returned HTML."""
    result = result.strip()
    result = re.sub(
        r"^```(?:json)?\s*|\s*```$",
        "",
        result,
        flags=re.IGNORECASE,
    ).strip()

    try:
        return json.loads(result)
    except json.JSONDecodeError:
        repaired = []
        in_string = False
        index = 0

        while index < len(result):
            char = result[index]
            code = ord(char)
            if in_string:
                if char == '"':
                    repaired.append(char)
                    in_string = False
                elif char == "\\":
                    next_char = result[index + 1] if index + 1 < len(result) else ""
                    valid_escape = bool(next_char) and next_char in '"\\/bfnrt'
                    valid_unicode = (
                        next_char == "u"
                        and index + 5 < len(result)
                        and all(c in "0123456789abcdefABCDEF" for c in result[index + 2:index + 6])
                    )
                    if valid_escape or valid_unicode:
                        repaired.append(char)
                        if next_char:
                            repaired.append(next_char)
                            index += 1
                            if valid_unicode:
                                repaired.extend(result[index + 1:index + 5])
                                index += 4
                    else:
                        # Preserve a literal backslash from HTML, CSS, or JavaScript.
                        repaired.append(r"\\")
                elif code < 32:
                    replacements = {
                        "\n": r"\n",
                        "\r": r"\r",
                        "\t": r"\t",
                        "\b": r"\b",
                        "\f": r"\f",
                    }
                    repaired.append(replacements.get(char, f"\\u{code:04x}"))
                else:
                    repaired.append(char)
            else:
                repaired.append(char)
                if char == '"':
                    in_string = True
            index += 1

        return json.loads("".join(repaired))


@app.post("/api/create")
def create_game():
    if not role_can_create():
        return jsonify(error="Game creation needs the normal or admin password."), 401

    if not API_KEY or API_KEY == "paste_your_own_api_key_here":
        return jsonify(error="Add your Gemini API key to .env first."), 500

    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "My Game"))[:60].strip() or "My Game"
    idea = str(data.get("idea", ""))[:2000].strip()
    genre = str(data.get("genre", "")).strip()
    build_android = data.get("build_android", True) is not False

    if genre not in GENRE_RULES:
        return jsonify(error="Choose a valid game genre from the list."), 400

    if not idea:
        return jsonify(error="Describe the game you want to create."), 400

    prompt = f"""
Create a complete, playable, single-file HTML5 2D browser game. This creator is 2D-only.

Game name: {name}
Selected genre (must be followed exactly): {genre}
Required gameplay for this genre: {GENRE_RULES[genre]}
Idea: {idea}

Hard requirements:
- Keep the selected genre as the main gameplay loop. The idea supplies the theme,
  characters, setting, and requested details; it does not replace the selected genre.
- Never turn a Puzzle, Racing, Simulation, Card, Board, Word, or other genre into
  a platformer or jump game. Add running/jumping/platforms only when Platformer is
  selected and the idea asks for them.
- If the idea mentions Candy Crush or similar, make a match-3 puzzle when Puzzle
  is selected. Do not make it a platformer.
- For a Candy Crush-style match-3 game, make the player swap neighboring pieces,
  clear groups of 3 or more, refill the board, count moves, and reach a target.
  Reject illegal swaps and reset the full board when the player restarts.
- Make a small but complete game instead of a large unfinished one. Every visible
  button must work; the start, win/lose, and restart states must all function.
- Keep movement, collisions, scoring, turns, and rules consistent with the
  selected genre. Do not add gravity or jumping to genres that do not need it.
- Build only in 2D using Canvas 2D or HTML elements. Do not use WebGL, Three.js,
  3D scenes, Godot, or external libraries.
- Use HTML, CSS and JavaScript only.
- No external libraries, external assets, or network dependencies.
- Make the selected genre playable with a clear title/start screen, instructions,
  objective, feedback, and restart. Add a score or progress indicator when it fits.
- Start with a polished, game-specific lobby/title screen before gameplay. Create
  an original logo/title treatment in inline SVG or Canvas, a themed background,
  a large working Play button, and a working How to Play/Controls panel. Gameplay
  starts only after Play is pressed; add pause/restart controls where suitable.
- Keep the logo and lobby inside the game so they appear in both browser and Android.
  Do not use a generic blank menu.
- Use keyboard controls for real-time games; use clear pointer/touch interaction
  for puzzle, card, and board games. Add large on-screen touch controls when useful.
- Make the game resize for mobile and desktop; keep controls visible and avoid
  covering important parts of the play area.
- Make the art look finished and intentional, not like a prototype. Do not use
  plain circles, rectangles, emoji, or text symbols as the final character or enemy art.
- Draw original sprite-like art with layered Canvas paths or inline SVG: add outlines,
  silhouettes, faces, clothing, shadows, highlights, and small color details. Use a
  consistent art direction and a limited palette that fits the game idea.
- For world or action games, add a detailed multi-layer background, foreground
  scenery, ground texture, depth, and subtle parallax or atmospheric effects.
- Animate characters and important objects with idle, movement, and action frames.
  Add tasteful particles and visual feedback for hits, pickups, attacks, and wins.
- Make the HUD and menus match the art style, with clear readable text and strong
  contrast. Scale Canvas for high-DPI screens so artwork stays crisp.
- If the idea is a zombie game, draw distinct detailed hero and zombie sprites,
  a ruined setting with debris and fog, and visible shooting and hit effects.
- Keep art effects smooth and performant; use procedural or inline art only, with
  no downloaded assets or network dependencies.
- Keep the game self-contained in one index.html file.
- Do not include explanations or markdown code fences.
- Return ONLY valid JSON in this exact format:
  {{"files":[{{"path":"index.html","content":"FULL HTML CODE"}}]}}
- The content must be complete, playable source code, not a plan.
"""

    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{MODEL}:generateContent"
    )

    try:
        response = requests.post(
            endpoint,
            params={"key": API_KEY},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.4,
                    "responseMimeType": "application/json",
                },
            },
            timeout=120,
        )

        if not response.ok:
            app.logger.error(
                "Gemini API error %s: %s",
                response.status_code,
                response.text[:1000],
            )
            return (
                jsonify(
                    error=(
                        f"Gemini API error ({response.status_code}). "
                        "Check the server terminal."
                    )
                ),
                502,
            )

        payload = response.json()
        candidates = payload.get("candidates", [])
        if not candidates:
            raise ValueError("Gemini returned no candidates.")

        parts = candidates[0].get("content", {}).get("parts", [])
        result = "".join(
            part.get("text", "")
            for part in parts
            if isinstance(part.get("text"), str)
        ).strip()
        if not result:
            raise ValueError("Gemini returned an empty response.")

        parsed = parse_gemini_json(result)
        files = parsed.get("files", [])
        if not isinstance(files, list) or len(files) != 1:
            raise ValueError("AI must return exactly one HTML file.")

        item = files[0]
        if not isinstance(item, dict):
            raise ValueError("AI returned an invalid file object.")

        rel = Path(str(item.get("path", "")))
        content = item.get("content")

        if (
            rel.is_absolute()
            or ".." in rel.parts
            or rel.as_posix() != "index.html"
            or not isinstance(content, str)
            or not content.strip()
            or len(content) > 1_000_000
        ):
            raise ValueError("AI returned an invalid HTML file.")

        safe_name = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-")[:40]
        safe_name = safe_name or "My-Game"
        folder_name = f"{safe_name}-{secrets.token_hex(3)}"

        folder = GAMES_DIR / folder_name
        folder.mkdir(parents=False, exist_ok=False)

        try:
            (folder / "index.html").write_text(content, encoding="utf-8")
        except Exception:
            try:
                (folder / "index.html").unlink(missing_ok=True)
                folder.rmdir()
            except OSError:
                pass
            raise

        apk_ready = False
        apk_error = None
        if build_android:
            try:
                build_android_apk(folder, name)
                apk_ready = True
            except Exception as exc:
                # Preserve the playable browser game if the Android SDK is unavailable.
                app.logger.exception("Android APK build failed for %s", folder_name)
                apk_error = str(exc)[:1200]

        return jsonify(
            ok=True,
            name=name,
            folder=folder_name,
            files=["index.html"],
            play_url=f"/games/{folder_name}/index.html",
            apk_ready=apk_ready,
            apk_url=(f"/games/{folder_name}/android.apk" if apk_ready else None),
            apk_error=apk_error,
        )

    except requests.RequestException as exc:
        app.logger.error("Gemini request failed: %r", exc)
        return (
            jsonify(
                error="Could not connect to Gemini. Check internet and try again."
            ),
            502,
        )
    except (KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        app.logger.error("Game generation failed: %r", exc)
        return (
            jsonify(
                error="Game generation failed. Check the terminal and try again."
            ),
            500,
        )


@app.get("/games/<folder>/<path:filename>")
def play_game(folder, filename):
    if not role_can_create():
        return jsonify(error="Playing games needs the normal or admin password."), 401

    if not re.fullmatch(r"[a-zA-Z0-9_-]+", folder):
        return "Invalid game folder", 400

    root = safe_game_folder(folder)
    if root is None:
        return "Game not found", 404

    if filename != "index.html":
        return "File not found", 404

    return send_from_directory(root, "index.html")


@app.get("/games/<folder>/android.apk")
def download_game_apk(folder):
    if not role_can_download():
        return jsonify(error="Game downloads need the download or admin password."), 401

    if not re.fullmatch(r"[a-zA-Z0-9_-]+", folder or ""):
        return "Invalid game folder", 400

    root = safe_game_folder(folder)
    if root is None or not (root / "android.apk").is_file():
        return "APK not found. Generate this game again with Android APK enabled.", 404

    return send_from_directory(
        root,
        "android.apk",
        as_attachment=True,
        download_name=f"{folder}.apk",
        mimetype="application/vnd.android.package-archive",
    )


@app.get("/games/<folder>/download")
def download_game(folder):
    if not role_can_download():
        return jsonify(error="Game downloads need the download or admin password."), 401
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", folder or ""):
        return "Invalid game folder", 400
    root = safe_game_folder(folder)
    if root is None or not (root / "index.html").is_file():
        return "Game not found", 404

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.write(root / "index.html", arcname="index.html")
    archive.seek(0)
    return send_file(
        archive,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{folder}.zip",
    )


if __name__ == "__main__":
    # Allow a phone on the same Wi-Fi network to open the studio and download APKs.
    app.run(host="0.0.0.0", port=5000, debug=False)
