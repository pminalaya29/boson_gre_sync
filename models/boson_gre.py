import base64
import hashlib
import json
import logging

from odoo import api, fields, models

from odoo.addons.boson_gre_sunat.services.gre_xml_builder import _issue_time

_logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DOCUMENT_EVENT = "gre.document.updated"
DOCUMENT_BACKFILL_EVENT = "gre.document.backfill"

# Cambios que merecen una foto nueva para el VPS.
TRACKED_FIELDS = {
    "state", "number", "sunat_ticket", "xml_signed_attachment_id", "cdr_zip_attachment_id",
    "sunat_accepted_at", "sunat_voided_at", "ticket_check_failures", "sunat_response_message",
    "receiver_name", "receiver_doc_type", "receiver_doc_number",
}

BACKFILL_PROGRESS_PARAM = "boson_gre_sync.backfill_last_gre_id"
BACKFILL_DISPATCH_PROGRESS_PARAM = "boson_gre_sync.backfill_last_picking_id"
BACKFILL_BATCH_PARAM = "boson_gre_sync.backfill_batch"
DEFAULT_BACKFILL_BATCH = 200

RELATED_TYPES = {
    "account.move": "invoice",
    "boson.stock.movement": "movement",
    "boson.purchase.receipt": "purchase_order",
    "stock.picking": "picking",
}


def _utc(value):
    return fields.Datetime.to_string(value) if value else None


def _payload_hash(payload):
    """Huella de la foto sin los Base64 (los archivos cuentan por su SHA-256)."""
    data = dict(payload)
    document = data.pop("electronic_document", None) or {}
    data["_files"] = {
        key: hashlib.sha256(value.encode()).hexdigest()
        for key, value in document.items() if key.endswith("_base64") and value
    }
    data.pop("revision", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


class BosonGre(models.Model):
    _inherit = "boson.gre"

    sync_revision = fields.Integer(string="Revisión enviada al VPS", copy=False, readonly=True)
    sync_hash = fields.Char(copy=False, readonly=True)
    sync_last_at = fields.Datetime(string="Enviada al VPS", copy=False, readonly=True)

    # ------------------------------------------------------------------
    # Disparo: cada cambio relevante de una guía con número
    # ------------------------------------------------------------------
    def write(self, vals):
        result = super().write(vals)
        if not self.env.context.get("boson_gre_sync_skip") and TRACKED_FIELDS.intersection(vals):
            self.filtered("number")._boson_sync_enqueue()
        return result

    def _boson_sync_enqueue(self, event_type=DOCUMENT_EVENT, force=False):
        """Crea el evento para el VPS si la foto cambió. Nunca rompe la operación
        que la originó: un fallo queda en el log y lo recupera la conciliación."""
        SyncEvent = self.env["boson.sync.event"].sudo()
        store_code = SyncEvent._get_store_code()
        created = self.env["boson.sync.event"]
        for gre in self.sudo():
            # Sin número o en borrador (paso intermedio al confirmar): no viaja.
            if not gre.number or gre.ui_stage == "draft":
                continue
            try:
                with self.env.cr.savepoint():
                    payload = gre._boson_sync_payload()
                    digest = _payload_hash(payload)
                    if not force and digest == gre.sync_hash:
                        continue
                    revision = gre.sync_revision + 1
                    payload["revision"] = revision
                    identity = payload["identity"]
                    suffix = ":bf" if event_type == DOCUMENT_BACKFILL_EVENT else ""
                    key = "gre:%s:%s:%s:%s:r%s%s" % (
                        identity["ruc"], identity["document_type"], identity["series"],
                        identity["number"], revision, suffix,
                    )
                    created |= SyncEvent.create_once({
                        "event_type": event_type,
                        "store_code": store_code,
                        "local_model": "boson.gre",
                        "local_id": gre.id,
                        "payload": payload,
                        "status": "pending",
                        "idempotency_key": key,
                    })
                    gre.with_context(boson_gre_sync_skip=True, boson_gre_system_write=True).write({
                        "sync_revision": revision,
                        "sync_hash": digest,
                        "sync_last_at": fields.Datetime.now(),
                    })
            except Exception:  # noqa: BLE001 - la conciliación lo reintenta
                _logger.exception("No se pudo preparar el envío al VPS de la GRE %s", gre.id)
                continue
            if gre.picking_id and event_type == DOCUMENT_EVENT:
                gre.picking_id._boson_sync_dispatch()
        return created

    # ------------------------------------------------------------------
    # Foto de la guía (contrato v1, ver boson_gre_receiver/docs)
    # ------------------------------------------------------------------
    def _boson_sync_payload(self):
        self.ensure_one()
        company = self.company_id.sudo()
        ruc = (company.vat or "").strip()
        doc_label, doc_number = self._recipient_document()
        recipient = self.recipient_partner_id.commercial_partner_id
        origin = self._print_place("origin")
        destination = self._print_place("destination")
        destination["store_code"] = self.destination_store_id.code if self.destination_store_id else ""
        carrier = None
        if self.transport_mode == "01" and not self.is_m1_l_vehicle and self.carrier_partner_id:
            commercial = self.carrier_partner_id.commercial_partner_id
            carrier = {"ruc": (commercial.vat or "").strip(), "name": commercial.name or ""}
        driver = vehicle = None
        if self.transport_mode == "02":
            if self.driver_id:
                driver = {
                    "name": " ".join(p for p in [self.driver_id.name, self.driver_id.last_name] if p),
                    "document_number": self.driver_id.document_number or "",
                    "license": self.driver_id.license_number or "",
                }
            if self.vehicle_id:
                vehicle = {"plate": self.vehicle_id.plate or ""}
        related = []
        for doc in self._get_related_documents():
            doc_type = RELATED_TYPES.get(doc.get("model"))
            if doc.get("model") == "sale.order":
                label = doc.get("label") or ""
                doc_type = "web_order" if "web" in label.lower() else (
                    "quotation" if "cotiz" in label.lower() else "sale_order")
            related.append({"type": doc_type or "other", "label": doc.get("label") or "", "ref": doc.get("ref") or ""})
        picking = self.picking_id.sudo()
        signed_raw = base64.b64decode(self.xml_signed_attachment_id.datas) if self.xml_signed_attachment_id.datas else b""
        payload = {
            "schema": SCHEMA_VERSION,
            "revision": self.sync_revision,
            "identity": {"ruc": ruc, "document_type": self.sunat_document_type or "09",
                         "series": self.series, "number": self.number},
            "local_gre_id": self.id,
            "stage": self.ui_stage,
            "state": self.state,
            "issue_date": fields.Date.to_string(self.issue_date) if self.issue_date else None,
            "issue_time": _issue_time(self) if self.issue_date else "",
            "transfer_start_date": fields.Date.to_string(self.transfer_start_date) if self.transfer_start_date else None,
            "reason_code": self.transfer_reason_code,
            "reason_description": self.transfer_reason_description or "",
            "transport_mode": self.transport_mode,
            "is_m1_l_vehicle": bool(self.is_m1_l_vehicle),
            "sender": {"ruc": ruc, "name": company.name or ""},
            "recipient": {"doc_type": doc_label, "number": doc_number, "name": recipient.name or ""},
            "receiver": {
                "name": self.receiver_name or "",
                "doc_type": self.receiver_doc_type or "",
                "number": self.receiver_doc_number or "",
            } if self.receiver_name else None,
            "origin": origin,
            "destination": destination,
            "carrier": carrier,
            "driver": driver,
            "vehicle": vehicle,
            "transport_extra": [list(item) for item in self._get_print_transport_extra()],
            "total_weight": self.total_weight,
            "package_count": self.package_count,
            "lines": [
                {
                    "sequence": index,
                    "sku": line.product_code or line.product_id.default_code or "",
                    "description": line.print_description or line.description or "",
                    "uom_code": line.sunat_uom_code or "",
                    "uom_label": line.uom_label or "",
                    "quantity": line.quantity,
                }
                for index, line in enumerate(self.line_ids, start=1)
            ],
            "related_documents": related,
            "picking": {
                "local_id": picking.id or 0,
                "name": picking.name or "",
                "picking_type_code": picking.picking_type_id.code or "",
                "date_done": _utc(picking.date_done),
                "movement_code": (picking.boson_movement_ref or "").strip()
                if picking and "boson_movement_ref" in picking._fields else "",
            },
            "sunat": {
                "ticket": self.sunat_ticket or "",
                "response_code": self.sunat_response_code or "",
                "status": self.sunat_status or "",
                "message": (self.sunat_response_message or "")[:4000],
                "sent_at": _utc(self.sunat_sent_at),
                "accepted_at": _utc(self.sunat_accepted_at),
                "voided_at": _utc(self.sunat_voided_at),
                "void_reason": self.sunat_void_reason or "",
                "qr_url": self.sunat_qr_url or "",
                "digest_value": self._get_print_digest_value(),
                "xml_sha256": hashlib.sha256(signed_raw).hexdigest() if signed_raw else "",
                "ticket_check_failures": self.ticket_check_failures,
                "emission_attempts": self.emission_attempts,
            },
        }
        document = {}
        if self.xml_signed_attachment_id.datas:
            document["xml_base64"] = self.xml_signed_attachment_id.datas.decode() \
                if isinstance(self.xml_signed_attachment_id.datas, bytes) else self.xml_signed_attachment_id.datas
            document["xml_filename"] = self.xml_signed_attachment_id.name or ""
        if self.cdr_zip_attachment_id.datas:
            document["cdr_base64"] = self.cdr_zip_attachment_id.datas.decode() \
                if isinstance(self.cdr_zip_attachment_id.datas, bytes) else self.cdr_zip_attachment_id.datas
            document["cdr_filename"] = self.cdr_zip_attachment_id.name or ""
        if document:
            payload["electronic_document"] = document
        return payload

    # ------------------------------------------------------------------
    # Conciliación y carga histórica
    # ------------------------------------------------------------------
    @api.model
    def cron_boson_sync_reconcile(self, limit=500):
        """Guías ya sincronizadas en vivo cuya foto cambió sin evento (por
        ejemplo, si falló su creación). No toca las nunca enviadas: esas van
        por la carga histórica."""
        gres = self.sudo().search(
            [("number", "!=", False), ("sync_revision", ">", 0)], order="id desc", limit=limit,
        )
        created = gres._boson_sync_enqueue()
        _logger.info("GRE→VPS conciliación: %s guías revisadas, %s eventos", len(gres), len(created))
        return len(created)

    @api.model
    def _boson_backfill_batch_size(self):
        value = self.env["ir.config_parameter"].sudo().get_param(BACKFILL_BATCH_PARAM)
        try:
            return max(int(value), 1) if value else DEFAULT_BACKFILL_BATCH
        except ValueError:
            return DEFAULT_BACKFILL_BATCH

    @api.model
    def cron_boson_sync_backfill(self):
        """Una tanda del histórico: guías con número nunca enviadas y, al
        terminar, las salidas que requieren GRE. Se puede retomar."""
        icp = self.env["ir.config_parameter"].sudo()
        batch = self._boson_backfill_batch_size()
        last_id = int(icp.get_param(BACKFILL_PROGRESS_PARAM) or 0)
        gres = self.sudo().search(
            [("number", "!=", False), ("id", ">", last_id), ("sync_revision", "=", 0)],
            order="id asc", limit=batch,
        )
        if gres:
            gres._boson_sync_enqueue(event_type=DOCUMENT_BACKFILL_EVENT)
            icp.set_param(BACKFILL_PROGRESS_PARAM, str(gres[-1].id))
            return {"documents": len(gres), "dispatches": 0, "done": False}
        sent = self.env["stock.picking"]._boson_sync_dispatch_backfill(batch)
        done = sent == 0
        if done:
            cron = self.env.ref("boson_gre_sync.ir_cron_boson_gre_sync_backfill", raise_if_not_found=False)
            if cron and cron.active:
                cron.sudo().active = False
            _logger.info("GRE→VPS: carga histórica terminada")
        return {"documents": 0, "dispatches": sent, "done": done}
