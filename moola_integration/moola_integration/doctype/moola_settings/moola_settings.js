frappe.ui.form.on('Moola Settings', {
  refresh(frm) {
    if (!frm.is_new()) {
      frm.add_custom_button(__('Sync From Date'), () => {
        frappe.prompt(
          [
            {
              fieldname: 'from_date',
              fieldtype: 'Date',
              label: 'From Date',
              reqd: 1,
              default: frappe.datetime.add_days(frappe.datetime.get_today(), -7),
            },
            {
              fieldname: 'advance_cursor',
              fieldtype: 'Check',
              label: 'Advance Cursor if Successful (optional)',
              default: 0,
            },
          ],
          (values) => {
            frappe.call({
              method: 'moola_integration.api.sync_from_date',
              args: {
                from_date: values.from_date,
                advance_cursor: values.advance_cursor ? 1 : 0,
              },
              freeze: true,
              freeze_message: __('Syncing from {0}…', [values.from_date]),
              callback: (r) => {
                if (r.message) {
                  const { fetched, created, skipped, errors } = r.message;
                  frappe.msgprint(
                    __('Sync complete.<br>Fetched: {0}<br>Created JE: {1}<br>Skipped: {2}<br>Errors: {3}',
                      [fetched, created, skipped, errors]
                    )
                  );
                  frm.reload_doc();
                }
              }
            });
          },
          __('Sync From Date'),
          __('Run')
        );
      }).addClass('btn-primary');
    }
  }
});
