import { For, Show, createResource, createSignal } from "solid-js";
import type { OutgoingShare } from "../lib/api";
import { api, ApiError } from "../lib/api";
import { ShareIcon } from "./icons";

/**
 * Pass a set to someone.
 *
 * Two ways out, in one dialog. An address sends the invitation directly; the
 * link is there because half the time the person is already in a chat window
 * and an email is a detour.
 *
 * The invitation is minted when the dialog opens, not when it is sent, so the
 * link exists to copy whether or not an address is filled in.
 *
 * Two shapes for the trigger. On a set page it is the one accent button of
 * the toolbar, with its label: it was a ghost button lost among five exports
 * that looked exactly like it. On a library card it is an icon, always shown
 * rather than on hover, because a phone has no hover.
 *
 * Both say whether the set is already shared, counted in people: an address,
 * or a link somebody took. Not in links — opening this dialog mints one, so
 * a count of links would only say who looked at the dialog.
 */
export default function ShareSet(props: {
  setId: string;
  title: string;
  variant?: "button" | "icon";
  /** People already holding it, from the library list (icon variant). */
  count?: number;
}) {
  const [link, setLink] = createSignal("");
  const [email, setEmail] = createSignal("");
  const [busy, setBusy] = createSignal(false);
  const [sent, setSent] = createSignal("");
  const [copied, setCopied] = createSignal(false);
  const [error, setError] = createSignal("");
  const [ttl, setTtl] = createSignal(0);
  const [suggestions, setSuggestions] = createSignal<
    { email: string; display_name: string }[]>([]);
  let typing: number | undefined;

  /**
   * Accounts matching what is typed, the way GitHub suggests collaborators.
   * One request per pause rather than per keystroke; three characters at
   * least, which the server enforces too.
   */
  function onType(value: string) {
    setEmail(value);
    window.clearTimeout(typing);
    const q = value.trim();
    if (q.length < 3) {
      setSuggestions([]);
      return;
    }
    typing = window.setTimeout(async () => {
      try {
        const found = await api.people(q);
        // Only if it still matches what is in the field: a slow answer for
        // "bob" must not land after the user has typed "bobby@".
        if (email().trim() === q) setSuggestions(found);
      } catch {
        setSuggestions([]);           // a hint, never a blocker
      }
    }, 200);
  }

  function pick(address: string) {
    setEmail(address);
    setSuggestions([]);
  }
  let dialog: HTMLDialogElement | undefined;

  // The set page asks at once, for the count on its button. A library card
  // already has the count from the list, so it asks only when opened.
  const [asked, setAsked] = createSignal(props.variant !== "icon");
  const [shares, { refetch }] = createResource(
    () => asked() && props.setId,
    (id) => api.setShares(id).catch(() => [] as OutgoingShare[]));

  const people = () => (shares() ?? []).filter(
    (s) => !s.revoked && (s.to_email || s.claims > 0));
  const count = () => (shares() ? people().length : props.count ?? 0);

  function state(share: OutgoingShare): string {
    if (share.claims > 0) {
      return share.to_email ? "added it" : `taken by ${share.claims}`;
    }
    if (share.expired) return "expired";
    if (share.dismissed) return "declined";
    return "not opened yet";
  }

  async function revoke(token: string) {
    setError("");
    try {
      await api.revokeShare(token);
      if (link().endsWith(`/shared/${token}`)) setLink("");
      await refetch();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not revoke that.");
    }
  }

  async function open() {
    setError("");
    setSent("");
    setCopied(false);
    dialog?.showModal();
    if (asked()) void refetch();
    else setAsked(true);
    if (link()) return;                 // one invitation per visit is plenty
    setBusy(true);
    try {
      const share = await api.shareSet(props.setId);
      setLink(share.link);
      setTtl(share.expires_in_days);
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
      setSuggestions([]);
      void refetch();
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
      <Show
        when={props.variant === "icon"}
        fallback={
          <button class="btn btn-primary btn-sm" onClick={open}
                  title={count() ? `Shared with ${count()}` : "Share this set"}>
            <ShareIcon />
            Share
            <Show when={count()}>
              <span class="share-count">· {count()}</span>
            </Show>
          </button>
        }
      >
        <button class="btn-icon share-icon-btn" classList={{ on: count() > 0 }}
                onClick={open}
                title={count() ? `Shared with ${count()} — share again`
                               : "Share with someone"}
                aria-label="Share with someone">
          <ShareIcon />
          <Show when={count()}>
            <span class="share-dot" aria-hidden="true" />
          </Show>
        </button>
      </Show>

      <dialog class="picker" ref={dialog}>
        <div class="picker-head">
          <div>
            <div class="picker-title">Share this set</div>
            <div class="tiny faint">{props.title}</div>
          </div>
          <button class="btn btn-ghost btn-sm" onClick={() => dialog?.close()}>
            Close
          </button>
        </div>

        <form class="row" onSubmit={send}>
          <div class="share-email">
            <input
              class="signin-input"
              type="email"
              placeholder="their email or name"
              autocomplete="off"
              value={email()}
              onInput={(e) => onType(e.currentTarget.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape" && suggestions().length) {
                  e.preventDefault();       // close the list, not the dialog
                  setSuggestions([]);
                }
              }}
            />
            <Show when={suggestions().length > 0}>
              <div class="share-suggestions" role="listbox">
                <For each={suggestions()}>
                  {(person) => (
                    <button type="button" class="share-suggestion"
                            role="option" onClick={() => pick(person.email)}>
                      <span>{person.display_name}</span>
                      <span class="tiny faint">{person.email}</span>
                    </button>
                  )}
                </For>
              </div>
            </Show>
          </div>
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

        <Show when={people().length > 0}>
          <div class="share-people">
            <div class="tiny faint">Shared with</div>
            <For each={people()}>
              {(share) => (
                <div class="share-person">
                  <span class="share-person-who">
                    {share.to_email || "Link"}
                  </span>
                  <span class="tiny faint">{state(share)}</span>
                  <div class="spacer" />
                  <button class="btn btn-ghost btn-sm"
                          onClick={() => revoke(share.token)}
                          title="The link stops working. Copies already made stay.">
                    Revoke
                  </button>
                </div>
              )}
            </For>
          </div>
        </Show>

        <div class="tiny faint share-note">
          {/* The part worth being explicit about: they get their own copy, so
              nothing you do afterwards reaches them and nothing they do
              reaches you. */}
          Whoever opens it gets their own copy, audio included — theirs to
          keep, star and delete. Deleting yours does not touch theirs.
          <Show when={ttl() > 0}>
            {" "}A link nobody opens stops working after {ttl()} days.
          </Show>
        </div>

        <Show when={error()}>
          <div class="tiny" style={{ color: "var(--warn)" }}>{error()}</div>
        </Show>
      </dialog>
    </>
  );
}
