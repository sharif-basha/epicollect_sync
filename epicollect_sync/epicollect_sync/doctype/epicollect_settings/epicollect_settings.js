frappe.ui.form.on("Epicollect Settings", {
	refresh(frm) {
		const queue = (method, freeze_message) => {
			// Save first, so the job runs with the settings shown on screen.
			const saved = frm.is_dirty() ? frm.save() : Promise.resolve();
			saved
				.then(() => frappe.call({ method, freeze: true, freeze_message }))
				.then((r) => {
					const res = r.message || {};
					frappe.show_alert({ message: __(res.message || ""), indicator: res.queued ? "green" : "orange" }, 7);
				});
		};
		frm.add_custom_button(__("Sync Now"), () => queue("epicollect_sync.sync.sync_now", __("Queuing sync...")));
		frm.add_custom_button(__("Create Fields"), () => {
			frappe.confirm(__("Scan Epicollect and create one field per question on Epicollect Entry. This can take several minutes. Continue?"), () =>
				queue("epicollect_sync.sync.create_fields", __("Queuing field creation...")));
		});
		frm.add_custom_button(__("Reset Checkpoint"), () => {
			frappe.confirm(__("The next sync will re-fetch every entry. Continue?"), () => {
				frappe.call({ method: "epicollect_sync.sync.reset_checkpoint" }).then(() => {
					frm.reload_doc();
					frappe.show_alert({ message: __("Checkpoint cleared."), indicator: "green" });
				});
			});
		});
	},
});
