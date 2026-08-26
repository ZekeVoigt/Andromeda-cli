"""Getting a hosted runner, without touching a hosting provider's dashboard.

`andromeda cloud up <endpoint>` is the difference between "we have cloud jobs"
and "users have cloud jobs". Everything underneath it worked before this
existed; registering a runner was a hand-written database call, which is a
feature exactly one person can use.

**Two credentials are minted, each shown once.**

The **device token** is the important one, and the reason it is minted rather
than reused is a bug this codebase has already fixed: `getDeviceByDeviceId`
takes `.first()` off its index, so one token across two machines means either
row can answer, and authentication then breaks for a machine that was just told
it was paired. A runner authenticates as *itself* — a `hosted` device, narrower
than a laptop's, which reaches the inference relay and the cloud routes and
nothing else.

The **fire secret** is symmetric: the server signs fires with it and the runner
verifies them. It therefore exists in two places by construction, sealed at rest
on one side and a platform secret on the other. `serve.VERIFIERS` is the seam
for an asymmetric replacement, which is worth taking before anyone other than
the operator hosts a runner.

Neither is recoverable afterwards. A route that could re-read them would make a
database dump enough to impersonate a runner, which is the whole property
sealing provides.
"""

from __future__ import annotations

from andromeda_agent import cloud_client

from .. import config as config_module
from .. import output


def _endpoint() -> tuple[str, str, str]:
    credentials = config_module.load_credentials()
    base = credentials.base_url or config_module.load().get("base_url", "")
    return base, credentials.device_token, credentials.device_id


def up(callback_url: str, provider: str = "modal") -> int:
    """Register a runner and print exactly what to run next."""
    if not callback_url:
        output.fail(
            "Which runner?",
            "Deploy one first — `modal deploy cli/modal_app.py` prints its URL — "
            "then `andromeda cloud up <that url>`.",
        )
        return 2

    base, token, device = _endpoint()
    try:
        answer = cloud_client.provision_machine(base, token, device, callback_url, provider)
    except cloud_client.CloudUnavailable as exc:
        output.fail("Could not provision a runner.", str(exc))
        return 2

    output.ok(f"Registered a {provider} runner at {answer.get('callbackUrl')}")
    output.info("")
    output.console.print(
        "  [yellow]These two secrets are shown once and are not stored anywhere "
        "you can read them back. Set them on the runner now.[/yellow]"
    )
    output.info("")
    output.console.print("  [dim]modal secret create andromeda-fire \\\\[/dim]")
    output.console.print(
        f"  [dim]  ANDROMEDA_FIRE_SECRET={answer.get('fireSecret')}[/dim]"
    )
    output.info("")
    output.console.print("  [dim]modal secret create andromeda-runner \\\\[/dim]")
    output.console.print(f"  [dim]  ANDROMEDA_BASE_URL={base} \\\\[/dim]")
    output.console.print(
        f"  [dim]  ANDROMEDA_DEVICE_TOKEN={answer.get('deviceToken')} \\\\[/dim]"
    )
    output.console.print(f"  [dim]  ANDROMEDA_DEVICE_ID={answer.get('deviceId')}[/dim]")
    output.info("")
    output.info("  then `andromeda cron push` to arm the jobs you already have")
    # Said because somebody who wired a runner by hand earlier will otherwise
    # leave a laptop's credential on it, which is exactly the shared-token
    # problem this command exists to avoid.
    output.info(
        "  this runner has its own credential — remove any you set by hand"
    )
    return 0


def status() -> int:
    """Whether a runner exists, and whether it is answering."""
    base, token, device = _endpoint()
    try:
        machine = cloud_client.machine_status(base, token, device)
    except cloud_client.CloudUnavailable as exc:
        output.fail("Could not read your runner's status.", str(exc))
        return 2

    if not machine:
        output.info("  no hosted runner. `andromeda cloud up <url>` registers one.")
        return 0

    marks = {
        "ready": "[green]ready[/green]",
        "unreachable": "[red]unreachable[/red]",
        "provisioning": "[yellow]provisioning[/yellow]",
        "stopped": "[dim]stopped[/dim]",
    }
    output.console.print(
        f"  {marks.get(str(machine.get('status')), machine.get('status'))}  "
        f"[cyan]{machine.get('provider')}[/cyan]  [dim]{machine.get('callbackUrl')}[/dim]"
    )
    failures = int(machine.get("consecutiveFailures") or 0)
    if failures:
        output.console.print(
            f"  [yellow]{failures} fire(s) in a row could not be delivered[/yellow]"
        )
    if machine.get("lastError"):
        output.console.print(f"  [dim]{machine.get('lastError')}[/dim]")
    # A stopped runner is not a broken one. Saying so stops somebody debugging
    # the single most normal state this system has.
    output.info(
        "\n  A runner that is asleep is working correctly — it wakes on a fire."
    )
    return 0


def down(yes: bool = False) -> int:
    """Stop the runner from being used, and say what is left to do by hand.

    Two things this deliberately does **not** do.

    It does not delete the jobs. Somebody tearing down a runner is usually
    moving hosts, not abandoning their automations, and a command that silently
    deleted a month of job definitions would be unforgivable for a saving of one
    later command. They are paused with the reason, and `cloud up` plus `cron
    push` brings them back.

    It does not destroy the container app. This program has no credentials for
    a hosting provider and should not: the blast radius of a bug in a teardown
    path that can delete infrastructure is not worth the convenience. It prints
    the one command instead.
    """
    base, token, device = _endpoint()
    try:
        machine = cloud_client.machine_status(base, token, device)
    except cloud_client.CloudUnavailable as exc:
        output.fail("Could not read your runner's status.", str(exc))
        return 2

    if not machine:
        output.info("  no hosted runner to tear down.")
        return 0

    if not yes:
        # Printed before, not after. The prompt is the thing being consented to,
        # the same rule `cron approve` follows.
        output.console.print(
            f"  This will disarm every cloud job and revoke the credential on "
            f"[cyan]{machine.get('callbackUrl')}[/cyan]."
        )
        output.console.print(
            "  [dim]Your jobs are kept and paused, not deleted.[/dim]"
        )
        output.fail("Not done.", "Add --yes when you mean it.")
        return 2

    try:
        answer = cloud_client.teardown_machine(base, token, device)
    except cloud_client.CloudUnavailable as exc:
        output.fail("Could not tear the runner down.", str(exc))
        return 2

    output.ok(
        f"Disarmed {answer.get('disarmed', 0)} job(s) and revoked the runner's "
        "credential."
    )
    output.info("  your jobs are paused, not deleted — `cloud up` restores them")
    output.info("")
    output.info("  the container itself is yours to remove:")
    output.console.print("  [dim]modal app stop andromeda-runner[/dim]")
    return 0
