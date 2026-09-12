<!--
  - SPDX-FileCopyrightText: 2017 Nextcloud GmbH and Nextcloud contributors
  - SPDX-License-Identifier: GPL-3.0-or-later
-->
# GCC Talk iOS app

**Video & audio calls and chat for GCC on iOS**

GCC Talk is a branded build of the open-source [Nextcloud Talk](https://github.com/nextcloud/talk-ios) iOS client,
maintained by Baitshook. It connects to a Nextcloud server running the Talk (spreed) app and provides
end-to-end chat, audio and video calls, screen sharing, polls, and file sharing.

## Prerequisites

- [Nextcloud server](https://github.com/nextcloud/server) version 14 or higher (that fulfills [ATS requirements](https://developer.apple.com/library/archive/documentation/General/Reference/InfoPlistKeyReference/Articles/CocoaKeys.html#//apple_ref/doc/uid/TP40009251-SW57)).
- [Nextcloud Talk](https://github.com/nextcloud/spreed) version 4.0 or higher.
- Xcode 16 or newer.
- [CocoaPods](https://cocoapods.org/)

## Development setup

```bash
git clone --recurse-submodules https://github.com/arshadkhan5/Baitshook-talk-ios.git
cd Baitshook-talk-ios
pod install
open GccTalk.xcworkspace
```

Always open the `.xcworkspace`, not the `.xcodeproj`.

Pull Requests are checked with [SwiftLint](https://github.com/realm/SwiftLint). Install it locally to catch issues early.

## Project layout

| Target | Bundle identifier | Purpose |
| --- | --- | --- |
| `GccTalk` | `com.gcc.Talk` | Main app |
| `ShareExtension` | `com.gcc.Talk.ShareExtension` | Share sheet extension |
| `NotificationServiceExtension` | `com.gcc.Talk.NotificationServiceExtension` | Decrypts push notifications |
| `BroadcastUploadExtension` | `com.gcc.Talk.BroadcastUploadExtension` | Screen sharing (ReplayKit) |
| `GccTalkIntents` | `com.gcc.Talk.GccTalkIntents` | Siri / App Intents |
| `GccTalkTests`, `GccTalkUITests` | | Unit, integration and UI tests |

All targets share the app group `group.com.gcc.Talk`. The main app additionally uses
`group.com.gcc.apps` to exchange accounts with a companion Files app.

## Branding

Everything brand-specific lives in `GccTalk/Settings/NCAppBranding.m`:

- `talkAppName`, `filesAppName`, `copyright`
- `bundleIdentifier`, `groupIdentifier`, `appsGroupIdentifier`
- `pushNotificationServer`, `privacyURL`
- brand colors and logo handling

Logos and icons live in `GccTalk/Images.xcassets` (`AppIcon`, `gcc_talk`, `gcc_talk_logo`, `talk-20`).

The app registers the `gcctalk://` URL scheme (see `GccTalk/Info.plist` and `AppDelegate.m`).

## Push notifications

Talk push notifications are relayed through a push proxy. The upstream proxy at
`push-notifications.nextcloud.com` only serves apps signed by Nextcloud GmbH, so GCC Talk runs its own.
The proxy lives in [push-proxy/](push-proxy/README.md) together with deployment instructions;
`pushNotificationServer` in `NCAppBranding.m` points at `https://push.baitshook.com`.
For troubleshooting on the device side see [docs/notifications.md](docs/notifications.md).

## WebRTC library

The app uses Nextcloud's builds of the WebRTC library from
[talk-clients-webrtc](https://github.com/nextcloud-releases/talk-clients-webrtc).

## Running tests locally

Use `start-instance-for-tests.sh` to spin up a local Nextcloud instance with Talk installed, then run
the `GccTalk` scheme's tests in Xcode. See the `ci-*.sh` scripts for the individual steps.

## License

GCC Talk is licensed under the [GNU General Public License v3.0 or later](LICENSE), the same license as
Nextcloud Talk. Original copyright belongs to Nextcloud GmbH and the Nextcloud contributors listed in
[AUTHORS.md](AUTHORS.md). Modifications are copyright Baitshook.
