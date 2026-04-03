class PgClient:
    def save_batch_info(self, *args, **kwargs):
        pass

    def get_salesforce_object_id(self, table_name, table_sys_id) -> str:
        id_mappings = {
            "table": "incidents",
            "sys_id": "8cd98a8d5375b21081e131f0a0490ea8",
            "object_id": "500a500000Z27m2AAB",
        }
        if id_mappings["sys_id"] == table_sys_id:
            return id_mappings["object_id"]
        else:
            return None


pg_client = PgClient()







