import { Show, createSignal } from "solid-js";
import { api, ApiError } from "../lib/api";

/**
 * Pass a set to someone.
 *
 * Two ways out, in one dialog. An address sends the invitation directly; the
 * link is there because half the time the person is already in a chat window
 * and an email is a detour.
 *
 * The invitation is minted when the dialog opens, not when it is sent, so the
 * link exists to copy whether or not an address is filled in.
 */
export default function ShareSet(props: { setId: string; title: string }) {
  const [link, setLink] = createSignal("");
  const [email, setEmail] = createSignal("");
  const [busy, setBusy] = createSignal(false);
  const [sent, setSent] = createSignal("");
  const [copied, setCopied] = createSignal(false);
  const [error, setError] = createSignal("");
  let dialog: HTMLDialogElement | undefined;

  async function open() {
    setError("");
    setSent("");
    setCopied(false);
    dialog?.showModal();
    if (link()) return;                 // one invitation per visit is plenty
    setBusy(true);
    try {
      const share = await api.shareSet(props.setId);
      setLink(share.link);
    } catch (err) {
      setError(err instanceof ApiError
        ? err.message
        : "Could not create a link for this set.");
    } finally {
      setBusy(false);
    }
  }

  async function send(event: Event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      // A fresh invitation per recipient, so the same link is not passed
      // around between people who each end up claiming a different copy.
      const share = await api.shareSet(props.setId, email().trim());
      setSent(share.emailed
        ? `Sent to ${email().trim()}.`
        : `This server could not send the mail — the link below works.`);
      setLink(share.link);
      setEmail("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not send that.");
    } finally {
      setBusy(false);
    }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(link());
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard access can be refused; the field is selectable either way.
      setError("Copy it from the field — the clipboard was not available.");
    }
  }

  return (
    <>
      <button class="btn btn-ghost btn-sm" onClick={open}>Share</button>

      <dialog class="picker" ref={dialog}>
        <div class="picker-head">
          <div>
            <div class="eyebrow">Share</div>
            <div class="tiny faint">{props.title}</div>
          </div>
          <button class="btn btn-ghost btn-sm" onClick={() => dialog?.close()}>
            Close
          </button>
        </div>

        <form class="row" onSubmit={send}>
          <input
            class="signin-input"
            type="email"
            placeholder="their email"
            value={email()}
            onInput={(e) => setEmail(e.currentTarget.value)}
          />
          <button class="btn btn-primary btn-sm" type="submit"
                  disabled={busy() || !email().trim()}>
            {busy() ? "Sending…" : "Send"}
          </button>
        </form>

        <Show when={sent()}>
          <div class="tiny muted">{sent()}</div>
        </Show>

        <div class="share-link">
          <div class="tiny faint">Or send the link yourself</div>
          <div class="row">
            <input class="signin-input mono" readOnly value={link()}
                   onFocus={(e) => e.currentTarget.select()} />
            <button class="btn btn-ghost btn-sm" onClick={copy}
                    disabled={!link()}>
              {copied() ? "Copied" : "Copy"}
            </button>
          </div>
        </div>

        <div class="tiny faint share-note">
          {/* The part worth being explicit about: they get their own copy, so
              nothing you do afterwards reaches them and nothing they do
              reaches you. */}
          Whoever opens it gets their own copy — theirs to keep, star and
          delete. Deleting yours does not touch theirs.
        </div>

        <Show when={error()}>
          <div class="tiny" style={{ color: "var(--warn)" }}>{error()}</div>
        </Show>
      </dialog>
    </>
  );
}
