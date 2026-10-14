app_name = "synapse_core"
app_title = "Synapse Core"
app_publisher = "Nomen"
app_description = "Transactional Outbox + camada de execucao da Cancao da Lyra"
app_email = "dev@nomen.me"
app_license = "mit"
required_apps = ["erpnext"]

# Hook SINCRONO: roda dentro de Document.save/insert/delete, na transacao do documento de negocio.
doc_events = {
    "*": {
        "on_change": "synapse_core.outbox.on_change",
        "on_trash": "synapse_core.outbox.on_trash",
    }
}

scheduler_events = {
    "cron": {
        "* * * * *": [
            "synapse_core.outbox.dispatch_pending",
            "synapse_core.autonomia.heartbeat_tick",
        ],
        "17 3 * * *": ["synapse_core.outbox.purge"],
    }
}

after_install = "synapse_core.setup.after_install"
after_migrate = "synapse_core.setup.after_migrate"
