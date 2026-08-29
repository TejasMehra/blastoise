CREATE OR REPLACE FUNCTION timestamp_id(table_name text)
RETURNS bigint AS
$$
  DECLARE
    time_part bigint;
    sequence_base bigint;
    tail bigint;
  BEGIN
    time_part := (((date_part('epoch', now()) * 1000))::bigint << 16);
    sequence_base := (
      'x' || substr(md5(table_name || 'blastoise_validation_salt' || time_part::text), 1, 4)
    )::bit(16)::bigint;
    tail := ((sequence_base + nextval(table_name || '_id_seq')) & 65535);
    RETURN time_part | tail;
  END
$$ LANGUAGE plpgsql VOLATILE;
