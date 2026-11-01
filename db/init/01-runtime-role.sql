CREATE ROLE study_runtime LOGIN PASSWORD 'study-runtime-local';
GRANT CONNECT ON DATABASE study TO study_runtime;
GRANT USAGE ON SCHEMA public TO study_runtime;
