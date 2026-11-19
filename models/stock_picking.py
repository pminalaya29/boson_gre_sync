import hashlib
import json
import logging

from odoo import api, fields, models

from .boson_gre import BACKFILL_DISPATCH_PROGRESS_PARAM, SCHEMA_VERSION, _utc

_logger = logging.getLogger(__name__)

DISPATCH_EVENT = "gre.dispatch.updated"
DISPATCH_BACKFILL_EVENT = "gre.dispatch.backfill"


class StockPicking(models.Model):
    _inherit = "stock.picking"

    boson_gre_sync_revision = fields.Integer(copy=False, readonly=True)
    boson_gre_sync_hash = fields.Char(copy=False, readonly=True)

    def _action_done(self):
        result = super()._action_done()
        self.filtered(lambda p: p.state == "done")._boson_sync_dispatch()
        return result

    def action_boson_mark_delivered_to_carrier(self):
        result = super().action_boson_mark_delivered_to_carrier()
        self._boson_sync_dispatch()
        return result

    def _boson_sync_dispatch_payload(self):
        self.ensure_one()
        gre = self.boson_gre_id
        identity = None
        if gre and gre.number:
            identity = {
                "ruc": (gre.company_id.vat or "").strip(),
                "document_type": gre.sunat_document_type or "09",
                "series": gre.series,
                "number": gre.number,
            }
        return {
            "schema": SCHEMA_VERSION,
            "revision": self.boson_gre_sync_revision,
            "picking": {
                "local_id": self.id,
                "name": self.name or "",
                "origin": self.origin or "",
                "picking_type_code": self.picking_type_id.code or "",
                "date_done": _utc(self.date_done),
                "partner_name": self.partner_id.commercial_partner_id.name or self.partner_id.name or "",
                "movement_code": (self.boson_movement_ref or "").strip() if "boson_movement_ref" in self._fields else "",
            },
            "dispatch_state": self.boson_dispatch_authorization_state or False,
            "carrier_delivered_at": _utc(self.boson_carrier_delivered_at),
            "carrier_delivered_by": self.boson_carrier_delivered_by_id.name or "",
            "gre": {"identity": identity, "stage": gre.ui_stage} if identity else None,
        }

    def _boson_sync_dispatch(self, event_type=DISPATCH_EVENT):
        """Foto del despacho para el VPS, solo de salidas validadas que
        requieren GRE y solo si cambió. Nunca rompe la validación."""
        SyncEvent = self.env["boson.sync.event"].sudo()
        store_code = SyncEvent._get_store_code()
        sent = 0
        for picking in self.sudo():
            if picking.state != "done" or not picking.boson_dispatch_authorization_state:
                continue
            try:
                with self.env.cr.savepoint():
                    payload = picking._boson_sync_dispatch_payload()
                    data = dict(payload)
                    data.pop("revision", None)
                    digest = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
                    if digest == picking.boson_gre_sync_hash and event_type == DISPATCH_EVENT:
                        continue
                    revision = picking.boson_gre_sync_revision + 1
                    payload["revision"] = revision
                    suffix = ":bf" if event_type == DISPATCH_BACKFILL_EVENT else ""
                    SyncEvent.create_once({
                        "event_type": event_type,
                        "store_code": store_code,
                        "local_model": "stock.picking",
                        "local_id": picking.id,
                        "payload": payload,
                        "status": "pending",
                        "idempotency_key": "gre-dispatch:%s:%s:r%s%s" % (store_code, picking.id, revision, suffix),
                    })
                    picking.write({"boson_gre_sync_revision": revision, "boson_gre_sync_hash": digest})
                    sent += 1
            except Exception:  # noqa: BLE001
                _logger.exception("No se pudo preparar el envío al VPS del despacho %s", picking.id)
        return sent

    @api.model
    def _boson_sync_dispatch_backfill(self, batch):
        icp = self.env["ir.config_parameter"].sudo()
        last_id = int(icp.get_param(BACKFILL_DISPATCH_PROGRESS_PARAM) or 0)
        pickings = self.sudo().search(
            [
                ("id", ">", last_id),
                ("state", "=", "done"),
                ("boson_dispatch_authorization_state", "!=", False),
                ("boson_gre_sync_revision", "=", 0),
            ],
            order="id asc",
            limit=batch,
        )
        if not pickings:
            return 0
        pickings._boson_sync_dispatch(event_type=DISPATCH_BACKFILL_EVENT)
        icp.set_param(BACKFILL_DISPATCH_PROGRESS_PARAM, str(pickings[-1].id))
        return len(pickings)
