import { Show, createResource, createSignal } from "solid-js";
import { api, ApiError } from "../lib/api";

/**
 * Who you are, as far as this app is concerned.
 *
 * A short page on purpose. The only field anyone else ever sees is the name,
 * which signs a shared set — everything else is here because you might need
 * to change it, not because it is interesting.
 */

const MAX_AVATAR_BYTES = 200 * 1024;

export default function Profile() {
  const [profile, { refetch, mutate }] = createResource(() => api.profile());

  const [saving, setSaving] = createSignal(false);
  const [saved, setSaved] = createSignal(false);
  const [error, setError] = createSignal("");

  const [newEmail, setNewEmail] = createSignal("");
  const [code, setCode] = createSignal("");
  const [emailStep, setEmailStep] = createSignal<"idle" | "sent">("idle");
  const [emailBusy, setEmailBusy] = createSignal(false);

  async function save(event: Event) {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    const data = new FormData(form);
    setSaving(true);
    setError("");
    try {
      const updated = await api.saveProfile({
        first_name: String(data.get("first_name") ?? ""),
        last_name: String(data.get("last_name") ?? ""),
      });
      mutate(updated);
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2500);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save that.");
    } finally {
      setSaving(false);
    }
  }

  /**
   * Read the picture in the browser and send it as a data URI.
   *
   * One small image per account, so a file store would mean a path, a
   * sweeper and a way to serve it — three moving parts for something that
   * fits in a column. Resized here rather than server-side: shipping two
   * megabytes to have it shrunk is the part worth avoiding.
   */
  async function pickAvatar(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    setError("");
    try {
      const dataUri = await shrink(file, 256);
      if (dataUri.length > MAX_AVATAR_BYTES) {
        setError("That image is too large even after resizing.");
        return;
      }
      mutate(await api.saveProfile({ avatar: dataUri }));
    } catch {
      setError("That file could not be read as an image.");
    } finally {
      input.value = "";
    }
  }

  async function sendEmailCode(event: Event) {
    event.preventDefault();
    setEmailBusy(true);
    setError("");
    try {
      await api.requestEmailChange(newEmail().trim());
      setEmailStep("sent");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not send a code.");
    } finally {
      setEmailBusy(false);
    }
  }

  async function confirmEmail(event: Event) {
    event.preventDefault();
    setEmailBusy(true);
    setError("");
    try {
      await api.confirmEmailChange(code().trim());
      setEmailStep("idle");
      setNewEmail("");
      setCode("");
      refetch();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "That code did not work.");
    } finally {
      setEmailBusy(false);
    }
  }

  async function signOutEverywhere() {
    await api.logoutEverywhere();
    window.location.href = "/";
  }

  return (
    <div class="wrap">
      <Show when={profile()} fallback={<div class="tiny faint">Loading…</div>}>
        {(me) => (
          <div class="profile">
            <div class="eyebrow">Profile</div>

            <form class="profile-block" onSubmit={save}>
              <div class="profile-avatar-row">
                <Show
                  when={me().avatar}
                  fallback={
                    <div class="avatar avatar-blank">
                      {me().display_name.slice(0, 1).toUpperCase()}
                    </div>
                  }
                >
                  <img class="avatar" src={me().avatar} alt="" />
                </Show>
                <label class="btn btn-ghost btn-sm">
                  Change picture
                  <input type="file" accept="image/*" hidden
                         onChange={pickAvatar} />
                </label>
              </div>

              <label class="field">
                <span class="tiny faint">First name</span>
                <input class="signin-input" name="first_name"
                       value={me().first_name} maxlength="80" />
              </label>
              <label class="field">
                <span class="tiny faint">Last name</span>
                <input class="signin-input" name="last_name"
                       value={me().last_name} maxlength="80" />
              </label>
              <div class="row">
                <button class="btn btn-primary btn-sm" type="submit"
                        disabled={saving()}>
                  {saving() ? "Saving…" : "Save"}
                </button>
                <Show when={saved()}>
                  <span class="tiny muted">Saved</span>
                </Show>
                <span class="tiny faint">
                  This is the name on a set you share.
                </span>
              </div>
            </form>

            <div class="profile-block">
              <div class="tiny faint">Email</div>
              <div class="row">
                <span class="mono">{me().email}</span>
              </div>

              <Show when={me().pending_email}>
                <div class="tiny muted">
                  Waiting on the code sent to {me().pending_email}.
                </div>
              </Show>

              <Show
                when={emailStep() === "sent"}
                fallback={
                  <form class="row" onSubmit={sendEmailCode}>
                    <input
                      class="signin-input"
                      type="email"
                      placeholder="new address"
                      value={newEmail()}
                      onInput={(e) => setNewEmail(e.currentTarget.value)}
                      required
                    />
                    <button class="btn btn-ghost btn-sm" type="submit"
                            disabled={emailBusy() || !newEmail().trim()}>
                      {emailBusy() ? "Sending…" : "Change it"}
                    </button>
                  </form>
                }
              >
                <form class="row" onSubmit={confirmEmail}>
                  {/* The code goes to the new address, not this one: what is
                      being proved is that the new mailbox is reachable and
                      yours. Nothing moves until it arrives. */}
                  <input
                    class="signin-input signin-code mono"
                    inputmode="numeric"
                    maxlength="6"
                    placeholder="000000"
                    value={code()}
                    onInput={(e) =>
                      setCode(e.currentTarget.value.replace(/\D/g, ""))}
                    required
                  />
                  <button class="btn btn-primary btn-sm" type="submit"
                          disabled={emailBusy() || code().length < 6}>
                    Confirm
                  </button>
                  <button class="btn btn-ghost btn-sm" type="button"
                          onClick={() => setEmailStep("idle")}>
                    Cancel
                  </button>
                </form>
                <div class="tiny faint">
                  We sent a code to {newEmail()}. Your address changes when it
                  is entered, not before.
                </div>
              </Show>
            </div>

            <div class="profile-block">
              <div class="tiny faint">Sessions</div>
              <div class="row">
                <button class="btn btn-ghost btn-sm" onClick={signOutEverywhere}>
                  Sign out everywhere
                </button>
                <span class="tiny faint">
                  Ends every session, on every device. The answer to a lost one.
                </span>
              </div>
            </div>

            <Show when={error()}>
              <div class="tiny" style={{ color: "var(--warn)" }}>{error()}</div>
            </Show>
          </div>
        )}
      </Show>
    </div>
  );
}

/** Draw the picture into a square canvas and return it as a data URI. */
function shrink(file: File, size: number): Promise<string> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    const url = URL.createObjectURL(file);
    image.onload = () => {
      URL.revokeObjectURL(url);
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = size;
      const context = canvas.getContext("2d");
      if (!context) return reject(new Error("no canvas"));
      // Cover rather than fit: a letterboxed avatar in a circle looks like a
      // mistake, and cropping the edges of a face rarely does.
      const side = Math.min(image.width, image.height);
      context.drawImage(
        image,
        (image.width - side) / 2, (image.height - side) / 2, side, side,
        0, 0, size, size);
      resolve(canvas.toDataURL("image/jpeg", 0.82));
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("not an image"));
    };
    image.src = url;
  });
}
