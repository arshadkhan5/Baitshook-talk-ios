# GCC Talk push proxy

A small, self-hosted replacement for `push-notifications.nextcloud.com`.

Apple only delivers push notifications that are signed with the key of the
developer account that published the app. Nextcloud's proxy holds Nextcloud's
key, so it can only reach the official `com.nextcloud.Talk` app. GCC Talk is
`com.gcc.Talk` under your own account, so it needs a proxy that holds *your*
APNs key. This directory is that proxy.

It speaks the protocol documented in
[nextcloud/notifications `docs/push-v2.md`](https://github.com/nextcloud/notifications/blob/master/docs/push-v2.md):

| Endpoint | Caller | Purpose |
| --- | --- | --- |
| `POST /devices` | GCC Talk app | Store the device's APNs tokens, after verifying the server-signed device identifier |
| `DELETE /devices` | GCC Talk app | Remove the device |
| `POST /notifications` | Nextcloud server | Verify each push's signature and forward it to APNs (alert, VoIP call, or silent delete) |
| `GET /health` | You | Liveness check |

The proxy never sees message content. Subjects arrive RSA-encrypted for the
device and are forwarded as-is. It stores only APNs tokens and user public keys.

## 1. Apple Developer portal (one time)

1. **App ID.** Under *Identifiers*, make sure `com.gcc.Talk` exists with the
   *Push Notifications* capability enabled. (Xcode's automatic signing usually
   creates it the first time you run the app on a device.)
2. **APNs key.** Under *Keys*, click **+**, name it (e.g. "GCC Talk push"),
   tick **Apple Push Notifications service (APNs)**, register, and download the
   `AuthKey_XXXXXXXXXX.p8` file. Note the 10-character **Key ID**. The file can
   only be downloaded once, keep it safe.
3. Your **Team ID** is shown in the top right of the portal. It is `9Z63VM6NS2`
   in the Xcode project.

One key covers both regular pushes and VoIP (call) pushes.

## 2. Deploy

Any Linux box with Docker, a public IP and a DNS name works. Ports 80 and 443
must be reachable so Caddy can obtain a certificate; Nextcloud will refuse a
proxy URL that is not `https://`.

```bash
cd push-proxy
cp .env.example .env            # then edit: domain, key id, team id
cp ~/Downloads/AuthKey_XXXXXXXXXX.p8 ./AuthKey.p8
docker compose up -d --build
curl https://push.baitshook.com/health
```

### Recommended: on the Nextcloud server itself (Apache on Ubuntu)

`nc.baitshook.com` already runs Apache, so the proxy can live on the same
machine behind it. No new server needed.

1. Add a DNS **A record** `push.baitshook.com` → the server's IP
   (`161.97.129.123`), at the place you manage `baitshook.com` DNS.
2. On the server, install Docker if it is not there yet:
   `curl -fsSL https://get.docker.com | sudo sh`
3. Copy this `push-proxy` folder to the server (for example with `scp` or
   `git clone` of the repo), put `AuthKey.p8` and `.env` in it as described
   above, then start only the proxy, bound to localhost:

   ```bash
   cd push-proxy
   docker compose -f docker-compose.yml -f docker-compose.apache.yml up -d --build push-proxy
   curl http://127.0.0.1:8080/health
   ```

4. Add the Apache site and get a certificate:

   ```bash
   sudo cp apache/push.baitshook.com.conf /etc/apache2/sites-available/
   sudo a2enmod proxy proxy_http ssl headers
   sudo a2ensite push.baitshook.com
   sudo systemctl reload apache2
   sudo certbot --apache -d push.baitshook.com
   curl https://push.baitshook.com/health
   ```

   `certbot` is already installed if Nextcloud's certificate came from Let's
   Encrypt; otherwise `sudo apt install certbot python3-certbot-apache`.

### No server access at all: Fly.io (hosted, a few dollars a month)

The proxy is independent of the Nextcloud server, so it can run anywhere with
a public HTTPS address. Fly.io gives a container its own `https://<app>.fly.dev`
hostname, no DNS or certificates to manage. Needs the `fly` CLI on your Mac
(`brew install flyctl`) and a Fly account with a payment method.

```bash
cd push-proxy
fly auth login
fly launch --copy-config --no-deploy          # accept the app name or pick another; choose a region
fly volumes create pushdata --size 1          # same region as the app
fly secrets set APNS_KEY_ID=AB12CD34EF APNS_TEAM_ID=9Z63VM6NS2 APNS_KEY_PEM="$(cat ~/Downloads/AuthKey_AB12CD34EF.p8)"
fly deploy
fly status                                    # note the hostname, e.g. gcc-talk-push.fly.dev
curl https://gcc-talk-push.fly.dev/health
```

The key is passed as a secret (`APNS_KEY_PEM`) instead of a file. Then set
`pushNotificationServer` in `GccTalk/Settings/NCAppBranding.m` to that
`https://….fly.dev` hostname and rebuild the app. `fly.toml` keeps one machine
always running so pushes are never delayed by a cold start.

For sandbox testing from Xcode: `fly secrets set APNS_ENVIRONMENT=sandbox`,
and back to `production` before TestFlight.

### Alternative: a separate machine with the bundled Caddy

Use `docker compose up -d --build` (without the override) on any Linux box
where ports 80 and 443 are free. Caddy obtains the certificate on its own.

`data/devices.json` holds the registrations. Back it up if you care about not
forcing every user to re-open the app after a reinstall.

**Sandbox vs production.** Pushes for builds installed straight from Xcode go
through Apple's sandbox environment, App Store and TestFlight builds through
production. Set `APNS_ENVIRONMENT` accordingly. If you need both at the same
time, run a second copy of the proxy under another hostname with
`APNS_ENVIRONMENT=sandbox` and point your debug build at it.

## 3. Point the app at it

`pushNotificationServer` in `GccTalk/Settings/NCAppBranding.m` is already
`https://push.baitshook.com`. Change it if you use a different hostname, then
rebuild. The app sends this URL to the Nextcloud server when it logs in, and
the server sends pushes there from then on.

## 4. Verify end to end

On the Nextcloud server, for a user who is logged in on an iPhone:

```bash
sudo -u www-data php occ notification:test-push --talk <username>
```

You should see `Push notification sent successfully` there, a line like
`registered device …` and no errors in `docker compose logs push-proxy`, and a
notification on the phone within a few seconds.

If the server prints `No devices found for user`, remove the account from the
app and log in again; registration happens at login.

## 5. Server-side notes

- Nextcloud disables push entirely on instances with 1000 or more active users
  that have no Nextcloud Enterprise subscription. This check happens before the
  server contacts any proxy, including this one.
- Nextcloud 35 and later encrypt pushes with OAEP padding by default; older
  versions use PKCS#1 v1.5. The GCC Talk app handles both.
- The server treats devices listed in the proxy's `unknown` response as gone
  and deletes them, which is how stale tokens are cleaned up automatically.

## Running the tests

```bash
cd push-proxy
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

The tests emulate a Nextcloud server and an iOS device with freshly generated
RSA keys, so they exercise the real signature and hashing logic, and replace
APNs with a fake that records what would have been sent.
