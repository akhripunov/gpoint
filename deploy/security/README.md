# SSH host maintenance: authentication by existing keys

Status: local proposal. Application deployment does not install this file.
Production SSH is unchanged until separately authorized host maintenance.

The reviewed policy is `sshd-key-only.conf`. It disables password and keyboard
interactive authentication while keeping public-key authentication enabled.
It permits root login by key if effective Match rules and account settings
allow it. Preflight must prove a fresh key-only connection through the actual
release transport before changing the host. The policy does not create, replace
or copy any key. Apply separately to each project VPS.

## Before applying

1. Obtain explicit owner authorization for host SSH configuration maintenance.
   Keep the current key-authenticated administrator session open throughout.
2. Confirm a second fresh connection succeeds using the existing approved key
   with `PreferredAuthentications=publickey` and `PasswordAuthentication=no`.
3. Record `sshd -T` and the relevant `sshd -T -C` contexts (root, actual client
   address/hostname and server address/port). Inspect Match/Include precedence
   and other authorized SSH users. Do not lock out a legitimate password-only
   account without an approved replacement login method.
4. Back up only sshd configuration and drop-ins, with owner-only permissions,
   outside Git. Prepare rollback from the still-open administrator session.
   Never copy host private keys into the repository or audit records.

## Controlled apply

1. Install the reviewed policy as a root-owned mode0644 early drop-in such as
   `/etc/ssh/sshd_config.d/00-owner-key-only.conf`. Confirm that the primary
   configuration Includes that directory. Do not overwrite an unrelated file.
2. Run `sshd -t` and dump the relevant `sshd -T -C` contexts again. Require:
   public-key authentication enabled; password and keyboard-interactive
   authentication disabled; root login `prohibit-password` (or the equivalent
   normalized `without-password`); X11 forwarding disabled. OpenSSH uses the
   first obtained value for most options; Match/Include ordering can defeat a
   drop-in. If any assertion fails, restore the saved configuration before
   reloading. A static repository test does not prove effective host settings.
3. Reload the existing SSH service using the host's verified service unit.
   Do not stop/restart the service or close the current session for this step.
4. Confirm another fresh key-only SSH connection succeeds and effective root
   and other approved-user contexts still meet the policy. If verification
   fails, restore the saved configuration from the open session, validate it
   with `sshd -t`, reload and recheck access.
5. Record only flags, timestamps and key-connection success in the existing
   security issue. Do not record passwords, private keys or raw debug output.

No unattended installation, service reload or firewall mutation is provided.
Reference: [OpenSSH sshd_config manual](https://man.openbsd.org/sshd_config).
