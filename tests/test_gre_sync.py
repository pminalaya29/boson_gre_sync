from odoo.tests import tagged

from odoo.addons.boson_gre_sunat.tests.test_gre_fase_s import TestGreFaseS


@tagged("post_install", "-at_install")
class TestGreSync(TestGreFaseS):
    """Reutiliza la preparación de la Fase S (empresa, sede, configuración,
    SUNAT simulado). Sus tests no se repiten aquí."""

    test_validate_creates_confirms_and_queues = None
    test_full_cycle_accepted_and_protected = None
    test_ticket_failures_never_set_error = None
    test_send_connection_error_is_retried = None
    test_send_http_validation_error_is_not_retried = None
    test_missing_data_keeps_draft_without_blocking = None
    test_rejected_number_is_consumed = None
    test_void_requires_confirmation_in_sunat = None
    test_issue_time_never_utc_in_cron = None
    test_print_values = None

    def _events(self, event_type=None, local_model=None):
        domain = [("event_type", "like", "gre.%")]
        if event_type:
            domain.append(("event_type", "=", event_type))
        if local_model:
            domain.append(("local_model", "=", local_model))
        return self.env["boson.sync.event"].sudo().search(domain, order="id")

    def test_live_flow_sends_snapshots_with_revisions(self):
        picking = self._validate_out()
        gre = picking.boson_gre_id
        docs = self._events("gre.document.updated")
        self.assertEqual(len(docs), 1, "Confirmar la guía envía una foto")
        first = docs.payload
        self.assertEqual(first["stage"], "confirmed")
        self.assertEqual(first["revision"], 1)
        self.assertEqual(first["identity"]["series"], gre.series)
        self.assertEqual(first["identity"]["number"], gre.number)
        self.assertNotIn("electronic_document", first, "Sin archivos todavía")
        dispatch = self._events("gre.dispatch.updated")
        self.assertTrue(dispatch)
        self.assertEqual(dispatch[-1].payload["dispatch_state"], "processing")

        self._emit_and_check(gre)
        docs = self._events("gre.document.updated")
        last = docs[-1].payload
        self.assertEqual(last["stage"], "accepted")
        self.assertGreater(last["revision"], first["revision"])
        self.assertIn("xml_base64", last["electronic_document"])
        self.assertIn("cdr_base64", last["electronic_document"])
        self.assertEqual(last["sunat"]["digest_value"], "DIGEST123=")
        self.assertEqual([line["sku"] for line in last["lines"]], ["016238"])
        self.assertEqual(last["recipient"], {"doc_type": "DNI", "number": "12345678", "name": "CLIENTE DE EJEMPLO"})
        keys = docs.mapped("idempotency_key")
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(self._events("gre.dispatch.updated")[-1].payload["dispatch_state"], "authorized")

        count = len(self._events())
        gre.write({"notes": "nota interna"})
        gre.with_context(boson_gre_system_write=True).write({"ticket_check_failures": 0})
        self.assertEqual(len(self._events()), count, "Sin cambios reales no se envía nada")

        picking.action_boson_mark_delivered_to_carrier()
        self.assertEqual(self._events("gre.dispatch.updated")[-1].payload["dispatch_state"], "delivered")

    def test_draft_without_number_sends_only_dispatch(self):
        self.config.default_carrier_partner_id = False
        picking = self._validate_out()
        self.assertEqual(picking.boson_gre_id.ui_stage, "draft")
        self.assertFalse(self._events("gre.document.updated"), "Una guía sin número no viaja")
        dispatch = self._events("gre.dispatch.updated")
        self.assertEqual(dispatch[-1].payload["dispatch_state"], "pending_gre")
        self.assertIsNone(dispatch[-1].payload["gre"])

    def test_backfill_sends_history_once_and_stops(self):
        Gre = self.env["boson.gre"]
        icp = self.env["ir.config_parameter"].sudo()
        picking = self._validate_out()
        gre = picking.boson_gre_id
        # Simula una guía anterior al módulo: nunca enviada.
        self._events().unlink()
        gre.with_context(boson_gre_sync_skip=True).write({"sync_revision": 0, "sync_hash": False})
        picking.write({"boson_gre_sync_revision": 0, "boson_gre_sync_hash": False})
        icp.set_param("boson_gre_sync.backfill_last_gre_id", "0")
        icp.set_param("boson_gre_sync.backfill_last_picking_id", "0")
        cron = self.env.ref("boson_gre_sync.ir_cron_boson_gre_sync_backfill")
        cron.active = True

        result = Gre.cron_boson_sync_backfill()
        self.assertEqual(result["documents"], 1)
        backfill = self._events("gre.document.backfill")
        self.assertEqual(len(backfill), 1)
        self.assertTrue(backfill.idempotency_key.endswith(":bf"))
        result = Gre.cron_boson_sync_backfill()
        self.assertEqual(result["dispatches"], 1)
        self.assertEqual(len(self._events("gre.dispatch.backfill")), 1)
        result = Gre.cron_boson_sync_backfill()
        self.assertTrue(result["done"])
        self.assertFalse(cron.active, "Al terminar, el cron del histórico se apaga solo")
        self.assertEqual(len(self._events("gre.document.backfill")), 1, "No reenvía lo ya enviado")

    def test_reconcile_recovers_missed_change(self):
        picking = self._validate_out()
        gre = picking.boson_gre_id
        before = len(self._events("gre.document.updated"))
        # Un cambio que no pasó por write (por ejemplo, SQL o un fallo al crear el evento).
        self.env.cr.execute("UPDATE boson_gre SET sunat_status = 'cambio sin evento', package_count = 2 WHERE id = %s", [gre.id])
        gre.invalidate_recordset()
        self.env["boson.gre"].cron_boson_sync_reconcile()
        self.assertEqual(len(self._events("gre.document.updated")), before + 1)
        self.assertEqual(self._events("gre.document.updated")[-1].payload["package_count"], 2)
