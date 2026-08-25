from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from yaol import fail, record_exception, span

from gh_pool.fleet.runners.errors import RunnerError
from gh_pool.fleet.runners.gh import release_version
from gh_pool.fleet.runners.metrics import LAUNCH_DURATION
from gh_pool.fleet.runners.provision import launch_many
from gh_pool.fleet.runners.provision import shrink as shrink_fleet

if TYPE_CHECKING:
    from gh_pool.fleet.runners.models import Stats
    from gh_pool.fleet.runners.state import Ctx

log = logging.getLogger(__name__)


def scale(ctx: Ctx, stats: Stats, note: str, *, shrink: bool = False) -> None:
    with span("runners.scale", {"repo": ctx.slug, "scale.note": note}) as active:
        try:
            version = ctx.target.version or release_version()
        except RunnerError as exc:
            log.warning("%s: не выяснил версию раннера: %s", ctx.slug, exc)
            record_exception(exc)
            version = ""

        with ctx.scaling:
            if ctx.closing.is_set():
                return
            want = min(ctx.target.jobs, stats.assigned)
            have = ctx.fleet.size()
            need = want - have

            active.set_attributes({"scale.want": want, "scale.have": have})
            log.info("%s | %s: want=%s have=%s need=%s", stats, note, want, have, need)
            if need < 0:
                if shrink:
                    shrink_fleet(ctx, -need)
                return
            if not need or not version:
                return

            started = time.monotonic()
            sent = launch_many(ctx, need, version)
            spent = time.monotonic() - started
            LAUNCH_DURATION.record(spent, {"repo": ctx.slug})
            active.set_attribute("scale.launched", sent)
            if sent < need:
                fail(f"раннеров уехало {sent} из {need}")
            log.info(
                "%s: раскидал раннеров %s/%s за %.1f с",
                ctx.slug,
                sent,
                need,
                spent,
            )
