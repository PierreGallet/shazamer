import { createSignal, Show, onMount } from "solid-js";
import { api, ApiError } from "../lib/api";

/**
 * The sign-in screen: an address, then the code that arrives.
 *
 * Two steps rather than one form, because the second step needs the first to
 * have happened and a single form would have to explain that. The address is
 * kept visible in step two so a typo is obvious before waiting for a mail
 * that is never coming.
 */
export function SignIn(props: { onSignedIn: () => void }) {
  const [step, setStep] = createSignal<"email" | "code">("email");
  const [email, setEmail] = createSignal("");
  const [code, setCode] = createSignal("");
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal("");

  let emailInput: HTMLInputElement | undefined;
  let codeInput: HTMLInputElement | undefined;

  onMount(() => emailInput?.focus());

  async function sendCode(event: Event) {
    event.preventDefault();
    if (busy()) return;
    setError("");
    setBusy(true);
    try {
      await api.requestCode(email().trim());
      setStep("code");
      // The focus has to wait for the input to exist.
      queueMicrotask(() => codeInput?.focus());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not send a code.");
    } finally {
      setBusy(false);
    }
  }

  async function verify(event: Event) {
    event.preventDefault();
    if (busy()) return;
    setError("");
    setBusy(true);
    try {
      await api.verifyCode(email().trim(), code().trim());
      props.onSignedIn();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not sign in.");
      setCode("");
      codeInput?.focus();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="signin-wrap">
      <div class="signin">
        <span class="brand-mark signin-mark">
          <svg viewBox="0 0 24 24" width="17" height="17" fill="#fff">
            <rect x="3" y="10" width="2.4" height="4" rx="1.2" />
            <rect x="8" y="6.5" width="2.4" height="11" rx="1.2" />
            <rect x="13" y="4" width="2.4" height="16" rx="1.2" />
            <rect x="18" y="8.5" width="2.4" height="7" rx="1.2" />
          </svg>
        </span>

        <Show when={step() === "email"}>
          <h1 class="signin-title">Sign in to Shazamer</h1>
          <p class="signin-sub">
            No password. We send a code to your inbox.
          </p>
          <form onSubmit={sendCode}>
            <input
              ref={emailInput}
              class="signin-input"
              type="email"
              autocomplete="email"
              inputmode="email"
              placeholder="you@example.com"
              value={email()}
              onInput={(e) => setEmail(e.currentTarget.value)}
              required
            />
            <button class="btn btn-primary signin-btn" type="submit"
                    disabled={busy() || !email().trim()}>
              {busy() ? "Sending..." : "Send me a code"}
            </button>
          </form>
        </Show>

        <Show when={step() === "code"}>
          <h1 class="signin-title">Check your inbox</h1>
          <p class="signin-sub">
            We sent a six-digit code to <strong>{email()}</strong>. It expires
            in ten minutes.
          </p>
          <form onSubmit={verify}>
            <input
              ref={codeInput}
              class="signin-input signin-code mono"
              type="text"
              inputmode="numeric"
              autocomplete="one-time-code"
              maxlength="6"
              placeholder="000000"
              value={code()}
              onInput={(e) => {
                // Digits only: people paste the code with spaces around it.
                setCode(e.currentTarget.value.replace(/\D/g, ""));
              }}
              required
            />
            <button class="btn btn-primary signin-btn" type="submit"
                    disabled={busy() || code().length < 6}>
              {busy() ? "Checking..." : "Sign in"}
            </button>
          </form>
          <button class="signin-back" type="button"
                  onClick={() => { setStep("email"); setError(""); setCode(""); }}>
            Use a different address
          </button>
        </Show>

        <Show when={error()}>
          <p class="signin-error">{error()}</p>
        </Show>
      </div>
    </div>
  );
}
