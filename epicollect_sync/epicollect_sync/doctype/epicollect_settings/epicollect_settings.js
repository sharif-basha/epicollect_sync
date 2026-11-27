frappe.ui.form.on("Epicollect Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Sync Now"), () => {
			frappe.call({ method: "epicollect_sync.sync.sync_now", freeze: true, freeze_message: __("Queuing sync...") })
				.then(() => frappe.show_alert({ message: __("Sync queued. Follow it in Epicollect Sync Log."), indicator: "green" }));
		});
		frm.add_custom_button(__("Create Fields"), () => {
			frappe.confirm(__("Scan Epicollect and create one field per question on Epicollect Entry. This can take several minutes. Continue?"), () => {
				frappe.call({ method: "epicollect_sync.sync.create_fields" })
					.then(() => frappe.show_alert({ message: __("Field creation queued. You will get a message when it finishes."), indicator: "green" }));
			});
		});
		frm.add_custom_button(__("Reset Checkpoint"), () => {
			frappe.confirm(__("The next sync will re-fetch every entry. Continue?"), () => {
				frm.set_value("last_uploaded_at", "");
				frm.save();
			});
		});
	},
});
