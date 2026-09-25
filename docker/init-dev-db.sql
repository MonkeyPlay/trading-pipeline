-- Runs once, when the container's data volume is first created.
-- The throwaway database populate_mock_data.py writes to (DEV_DATABASE_URL).
CREATE DATABASE trading_pipeline_dev OWNER trading;
