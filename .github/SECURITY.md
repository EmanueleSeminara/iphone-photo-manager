# Security policy

## Reporting a vulnerability

**Please do not open a public issue.** Use GitHub's private reporting:
[*Security → Report a vulnerability*](https://github.com/EmanueleSeminara/iphone-photo-manager/security/advisories/new).

Expect a first reply within a week. If a fix is needed, the advisory is published together
with it.

## What counts as a vulnerability here

This application has no network surface, no server, no accounts and no credentials. It
runs on one machine, talks to one phone over a USB cable, and writes to one folder. So the
interesting failures are not the usual ones:

* **anything that can delete a photo from the phone whose local copy is not verified.**
  This is the worst outcome the project has. The gates are described in
  [`CONTRIBUTING.md`](../CONTRIBUTING.md); a way around any of them is a security bug;
* **any write to the phone's file system.** The AFC channel is read-only, deliberately and
  by static test. A path that writes through it is a bug even if it appears harmless;
* **anything that makes the app write outside its destination folder**, for example
  through a crafted `local_path` in a manifest. Manifests are ordinary SQLite files in a
  folder the user can edit, so their contents are treated as untrusted input;
* **anything that leaks the phone's data off the machine.** Nothing in this app should
  ever open a socket. A dependency that does is in scope;
* **rendering device-controlled text unescaped.** File names and device names come from
  the phone and are escaped before they reach the terminal.

Out of scope: the phone being unlocked, the user choosing a bad destination folder, and
anything that requires an attacker who already has your unlocked Mac.

## Handling your own data

If you are reporting a bug, do not attach:

* a manifest (`.ipm/manifest.db`) — it lists every file you imported;
* a copy of `Photos.sqlite` — it holds the metadata of every photo on your phone;
* your device UDID or serial number.

Redacted excerpts are fine, and are usually enough. **`<destination>/.ipm/last-run.log`
is meant to be attached**: it is the activity panel in plain text, and holds nothing from
the phone's database.

## Supported versions

The latest release on `main` is the supported one. This is a small project; there are no
backports.
