# Memento Android companion

This small Android shell opens the self-hosted Memento web application and
adds one capability normal mobile browsers cannot provide reliably: a native
rich clipboard item for pasting a formatted message into Android apps.

The app uses AndroidX `WebViewCompat.addWebMessageListener` with the single
allowed origin `https://memento.babypotatofarm.com`. External navigation opens
in the system browser and cannot access the bridge. The native clipboard item
contains styled text, semantic HTML, and a readable plain representation.

## Build

The project uses Android Gradle Plugin 8.9.2, Gradle 8.11.1, JDK 17, and
`compileSdk 35`.

```powershell
./gradlew.bat test lint assembleDebug
```

For a release build, provide signing material outside the repository:

```text
MEMENTO_ANDROID_KEYSTORE_FILE
MEMENTO_ANDROID_KEYSTORE_PASSWORD
MEMENTO_ANDROID_KEY_ALIAS
MEMENTO_ANDROID_KEY_PASSWORD
```

No credentials, transcript data, or signing keys belong in this directory.
