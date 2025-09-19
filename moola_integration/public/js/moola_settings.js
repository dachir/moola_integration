frappe.ui.form.on("Moola Settings", {
  refresh(frm) {
    if (!frm.is_new() && frm.doc.enabled) {
      frm.add_custom_button("Sync Now", () => {
        frappe.call({
          method: "moola_integration.api.sync_now",
          freeze: true,
          callback: (r) => frappe.msgprint(r.message || "Done")
        });
      });
    }
  }
});
