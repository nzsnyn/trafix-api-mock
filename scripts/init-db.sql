-- Create the separate database the test suite uses, so pytest never writes
-- into the simulator's records. The main 'parkways' database is created by
-- the postgres image from POSTGRES_DB.
CREATE DATABASE parkways_e2e OWNER trafix;
