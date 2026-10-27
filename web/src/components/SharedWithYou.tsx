import { For, Show, createSignal } from "solid-js";
import { api, ApiError, formatDuration } from "../lib/api";
import { inbox } from "../lib/inbox";
import { ShareIcon } from "./icons";

/**
 * Sets someone sent you, at the top of the library.
 *
 * A share addressed to you used to exist only as an email and a URL: open
 * the app any other way, or sign up after it was sent, and it was gone. Here
 * it waits until you take it or say no thanks. Declining only hides it; the
 * sender's link keeps working, because only they can take it back.
 */
export default function SharedWithYou(props: {
  onOpen: (setId: string) => void;
  onTaken: () => void;
}) {
  const [busy, setBusy] = createSignal<string | null>(null);
  const [error, setError] = createSignal("");

  async function take(token: string) {
    setBusy(token);
    setError("");
    try {
      const { set_id } = await api.claimShare(token);
      inbox.refresh();
      props.onTaken();
      props.onOpen(set_id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not add that set.");
    } finally {
      setBusy(null);
    }
  }

  async function dismiss(token: string) {
    setBusy(token);
    setError("");
    try {
      await api.dismissShare(token);
      inbox.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not dismiss that.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Show when={(inbox.shares() ?? []).length > 0}>
      <section class="stack shared-inbox">
        <div class="row">
          <span class="eyebrow chip-icon">
            <ShareIcon size={13} />
            Shared with you ({inbox.shares()!.length})
          </span>
        </div>
        <For each={inbox.shares()}>
          {(share) => (
            <div class="shared-inbox-card">
              <div class="set-card-body">
                <div class="set-card-title">{share.title}</div>
                <div class="row-wrap tiny faint">
                  <span>from {share.from_name || "someone"}</span>
                  <span>·</span>
                  <span class="mono">{share.track_count} tracks</span>
                  <Show when={share.duration}>
                    <span>·</span>
                    <span>{formatDuration(share.duration)}</span>
                  </Show>
                </div>
              </div>
              <div class="row">
                <button class="btn btn-primary btn-sm"
                        disabled={busy() === share.token}
                        onClick={() => take(share.token)}>
                  Add to my library
                </button>
                <button class="btn btn-ghost btn-sm"
                        disabled={busy() === share.token}
                        onClick={() => dismiss(share.token)}>
                  Dismiss
                </button>
              </div>
            </div>
          )}
        </For>
        <Show when={error()}>
          <div class="tiny" style={{ color: "var(--warn)" }}>{error()}</div>
        </Show>
      </section>
    </Show>
  );
}
