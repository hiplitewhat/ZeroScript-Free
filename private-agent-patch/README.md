# PrivateAgent — ZeroScript bridge patch

Makes [PrivateAgent](https://github.com/hipliteidk-glitch/private-agent) (the
Android screen agent) run against the **ZeroScript bridge** — the free, keyless
Roblox AI agent whose OpenAI-compatible HTTP API runs on the same device.

The sandbox agent could not push to the `private-agent` repo (its GitHub token
covers `ZeroScript-Free` only), so the change ships as a patch you apply and let
GitHub Actions build.

## Get it and build the APK

```sh
curl -O https://raw.githubusercontent.com/hipliteidk-glitch/ZeroScript-Free/arena/01a1003e-zeroscript-free/private-agent-patch/build-zeroscript-apk.sh
curl -O https://raw.githubusercontent.com/hipliteidk-glitch/ZeroScript-Free/arena/01a1003e-zeroscript-free/private-agent-patch/private-agent-zeroscript.patch
sh build-zeroscript-apk.sh
```

The script clones your fork, applies the patch on a branch called
`zeroscript-bridge`, pushes it, and pushes a `v1.0.3-zeroscript` tag — which is
what starts the APK build. Download the artifact from
<https://github.com/hipliteidk-glitch/private-agent/actions>
(`private-agent-android` → `app-universal-release.apk`, good for arm64, arm32
and x86_64).

## What it changes

| File | Change |
|---|---|
| `android/app/src/main/res/xml/network_security_config.xml` | **New.** Allows cleartext HTTP for loopback only (`localhost`, `127.0.0.1`, `::1`); every other host keeps the API 28+ HTTPS default |
| `android/app/src/main/AndroidManifest.xml` | Points the app at that config |
| `lib/services/ai_service.dart` | Defaults to `http://127.0.0.1:17614/v1` + model `zeroscript`; a loopback base URL needs no API key (`isLoopbackBaseUrl`), `isConfigured` and all three request guards follow the same rule |
| `lib/screens/settings_screen.dart` | ZeroScript preset chip; Fetch works without a key on loopback |
| `lib/screens/onboarding_screen.dart` | ZeroScript card, pre-selected on a fresh install, key field hidden, setup may finish while the bridge is stopped |
| `test/ai_service_test.dart` | Two tests for the loopback rule |
| `pubspec.yaml` | `1.0.3+2022` so the build is identifiable |

Verified before shipping: the patch applies cleanly to a pristine clone of
`main` (checked with `git apply --check`), both XML files parse, and the edits
change bracket counts by even amounts. There is **no** Dart/Flutter toolchain in
the sandbox, so the first CI run is the compile check — `flutter test` runs
before the APK is built.

## Use it

1. Start the bridge on the phone and confirm it is current:

   ```sh
   sh Termux_Start.sh
   curl -s localhost:17614/status | grep -o sse_streaming   # must print sse_streaming
   ```

2. Install the APK, tap through onboarding (already set to ZeroScript).
3. Keep the supported AI chat tab open so the bridge has a brain, or set
   `ZS_LLM_KEY` on the bridge for a fully headless one.
