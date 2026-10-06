# GloveMouse

Control your Windows PC with a gloved hand, using just your webcam. Made for
dental labs and clinics, where touching a mouse or keyboard with gloves on
isn't ideal.

- **Move** your gloved hand: the cursor follows
- **Quick pinch** (thumb + index): left click
- **Pinch and hold / move**: drag & drop, open your fingers to drop

## Download
Get **GloveMouse-windows.zip** from the [latest release](../../releases/latest),
unzip it, and run `GloveMouse.exe`. No installation needed.

Windows may show "Windows protected your PC" because the app isn't code-signed:
click **More info → Run anyway**. Each release lists SHA-256 checksums, and the
exe is built automatically by GitHub from the code in this repository.

## Privacy
- Camera images are processed live, in memory, and discarded immediately.
  **No video or picture is ever recorded, saved or sent anywhere.**
- The app has no internet features at all and works fully offline.
  You can check this yourself by blocking it in Windows Firewall: it keeps working.
- The only files it writes are its settings and a crash log, in
  `%APPDATA%\GloveMouse`.
- The full source code is in this repository for anyone to inspect.

## How to use
1. Launch the app. A preview window opens.
2. Press **F6**. The preview shrinks into a corner and stays on top.
3. Within 3 seconds, hold your gloved hand open, palm toward the camera.
   The app learns the glove colour and locks onto your hand.
4. Control the mouse. Press **F6** again to pause; starting again recalibrates,
   so you can change gloves anytime.

| Default key | Action |
|-----|--------|
| F6 | Start / Pause capture |
| F7 | Glove requirement ON / OFF |
| F8 | Settings |
| F9 | Quit |

The Settings page lets you change the shortcuts, which point of the hand moves
the cursor, user tracking (only the calibrating hand controls the mouse),
long-range mode, preview position and size, and sensitivity.

## Build from source
Install Python 3.11, then double-click `build.bat`. Results go to `dist\` and `release\`.

## License
MIT, see [LICENSE](LICENSE). Bundled third-party components (MediaPipe, OpenCV,
NumPy, Pillow, Python and others) keep their own licenses, listed in
`THIRD_PARTY_LICENSES.txt` inside the download.
