import { Show, createResource, createSignal } from "solid-js";
import { api, ApiError } from "../lib/api";

/**
 * Your own Soulseek account, for fetching tracks through it (RFC #137).
 *
 * Saved, it replaces the shared account for you entirely: searches and
 * downloads go through it, and if it fails you see that failure rather than
 * being quietly moved back onto everyone's account.
 *
 * The one-session warning is not small print. Soulseek accepts one login per
 * account, so while this app uses yours, your own Soulseek client is logged
 * out — and the other way round. People find that out the hard way otherwise.
 */
export default function SoulseekAccount() {
  const [account, { mutate }] = createResource(() => api.soulseekAccount());

  const [username, setUsername] = createSignal("");
  const [password, setPassword] = createSignal("");
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal("");

  async function save(event: Event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      mutate(await api.saveSoulseekAccount(username().trim(), password()));
      setPassword("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save that.");
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    setError("");
    try {
      await api.removeSoulseekAccount();
      mutate({ configured: false, username: null, last_ok_at: null,
               last_error: null });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not remove it.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="profile-block">
      <div class="tiny faint">Soulseek</div>

      <div class="tiny muted">
        Fetch tracks with your own Soulseek account instead of the shared one.
        Soulseek allows one session per account: while Shazamer uses yours,
        your Soulseek app is logged out, and the other way round. A dedicated
        account is best.
      </div>

      <Show when={account()?.last_error}>
        <div class="tiny" style={{ color: "var(--warn)" }}>
          {account()!.last_error}
        </div>
      </Show>

      <Show
        when={account()?.configured}
        fallback={
          <form class="row" onSubmit={save}>
            <input
              class="signin-input"
              placeholder="Soulseek username"
              autocomplete="off"
              value={username()}
              onInput={(e) => setUsername(e.currentTarget.value)}
              maxlength="64"
              required
            />
            <input
              class="signin-input"
              type="password"
              placeholder="password"
              autocomplete="new-password"
              value={password()}
              onInput={(e) => setPassword(e.currentTarget.value)}
              maxlength="128"
              required
            />
            <button class="btn btn-primary btn-sm" type="submit"
                    disabled={busy() || !username().trim() || !password()}>
              {busy() ? "Checking…" : "Save"}
            </button>
          </form>
        }
      >
        <div class="row">
          <span class="mono">{account()!.username}</span>
          <span class="tiny faint">Your fetches go through this account.</span>
          <button class="btn btn-ghost btn-sm" onClick={remove}
                  disabled={busy()}>
            Remove
          </button>
        </div>
      </Show>

      <Show when={error()}>
        <div class="tiny" style={{ color: "var(--warn)" }}>{error()}</div>
      </Show>
    </div>
  );
}
