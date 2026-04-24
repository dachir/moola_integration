frappe.ui.form.on('Moola Settings', {
  refresh(frm) {
    if (!frm.is_new()) {
      frm.add_custom_button(__('Sync By Period'), () => {
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
              fieldname: 'to_date',
              fieldtype: 'Date',
              label: 'To Date',
              reqd: 1,
              default: frappe.datetime.get_today(),
            },
            {
              fieldname: 'advance_cursor',
              fieldtype: 'Check',
              label: 'Advance Cursor if Successful (optional)',
              default: 0,
            },
          ],
          (values) => {
            if (values.to_date < values.from_date) {
              frappe.msgprint(__('To Date cannot be earlier than From Date'));
              return;
            }

            frappe.call({
              method: 'moola_integration.api.sync_by_period',
              args: {
                from_date: values.from_date,
                to_date: values.to_date,
                advance_cursor: values.advance_cursor ? 1 : 0,
              },
              freeze: true,
              freeze_message: __('Syncing from {0} to {1}…', [
                values.from_date,
                values.to_date,
              ]),
              callback: (r) => {
                if (r.message) {
                  const { fetched, created, skipped, errors } = r.message;
                  frappe.msgprint({
                      title: __("Moola Sync"),
                      message: `
                          <b>Sync has been queued successfully.</b><br><br>
                          Period: ${from_date} → ${to_date}<br><br>
                          Results will be available in:<br>
                          • Background Jobs<br>
                          • Moola Sync Log
                      `,
                      indicator: "blue"
                  });
                  frm.reload_doc();
                }
              },
            });
          },
          __('Sync By Period'),
          __('Run')
        );
      }).addClass('btn-primary');
    }
  },
});
