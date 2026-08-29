# frozen_string_literal: true
#
# Render the SQL a Rails migration runs, without guessing at it.
#
# Rails migrations are Ruby, and the SQL they run does not exist until
# ActiveRecord renders it against a live connection: `add_column` consults
# the adapter for the type name, `remove_index` looks the index up by its
# columns, and a `change_table` block only becomes statements once it is
# executed. There is no supported way to ask Rails what it *would* run --
# no --sql mode, no dry run -- so the only faithful answer is to run it and
# record what happened.
#
# What it is recorded with matters. Diffing the schema before and after
# would lose exactly the things a migration reviewer cares about: the
# catalog does not record that an index was built CONCURRENTLY, a backfill
# UPDATE leaves no trace in the schema at all, and whether the whole thing
# ran inside a transaction is not a property of the resulting tables. So
# this subscribes to `sql.active_record` instead, which is the notification
# ActiveRecord publishes for every statement it executes, and keeps the
# statements in the order they ran.
#
# The notification's payload has carried `:sql` and `:name` unchanged since
# Rails 3; versions since have added keys, never removed these two. `:name`
# is what separates the migration's own statements from ActiveRecord's
# bookkeeping: catalog introspection is tagged "SCHEMA", and BEGIN/COMMIT
# are tagged "TRANSACTION".
#
# This runs against a throwaway database that the caller has already
# decided is safe to write to. It never touches the database being
# assessed.

require 'json'

# Standard library a booting Rails application would have pulled in, and
# which gems therefore assume is present without requiring it themselves:
# activesupport <= 6.1 references ::Logger, scenic references ::TSort. This
# harness deliberately does not boot the application, so it has to supply
# them -- before ActiveRecord loads, because that is when the first of them
# is needed -- or those gems raise NameError on load.
%w[logger tsort set singleton benchmark].each do |lib|
  begin
    require lib
  rescue LoadError
    next
  end
end

require 'active_record'

request  = JSON.parse(File.read(ARGV[0]))
response = { 'ok' => false }

# Statements ActiveRecord runs to record that a migration happened. They are
# real, but they are not the migration, and reporting them would put an
# INSERT into every Rails verdict.
BOOKKEEPING = /\A\s*(INSERT INTO|DELETE FROM|SELECT)\s+"?(schema_migrations|ar_internal_metadata)"?/i

# Settings a tool read on its way past. strong_migrations asks for
# `server_version_num` and `lock_timeout` before it decides whether to
# object, and those SHOWs arrive in the stream tagged as ordinary
# statements rather than as schema queries. A SHOW takes no lock, touches
# no row and changes nothing, so it can never be the hazard a report is
# about -- unlike SET, which can be exactly that and is kept.
SETTING_READ = /\A\s*SHOW\s/i

# A leading backslash marks a psql meta-command. Spelled as a code point so
# the file survives being written through shells and heredocs.
PSQL_META = 92.chr

# Gems that add to the migration or schema DSL, mapped to the constant whose
# `.load` installs them where they do not install themselves on require.
# Without these an application's own schema.rb cannot always be loaded to
# build the pre-state, and a migration calling `safety_assured` raises
# NoMethodError. Every one is optional: an app that does not bundle it
# simply does not get it.
DSL_GEMS = {
  'strong_migrations' => nil,
  'hairtrigger'       => nil,
  'neighbor'          => nil,
  'fx'                => 'Fx',
  'scenic'            => 'Scenic',
}.freeze

def inline_binds(sql, binds, conn)
  # Model-driven DML arrives parameterised. The parameter values are not
  # interesting to a risk assessment, but a bare $1 is not parseable as a
  # complete statement by everything downstream, so the literals go back in.
  return sql if binds.nil? || binds.empty?
  binds.each_with_index.reduce(sql) do |acc, (value, index)|
    literal = value.nil? ? 'NULL' : conn.quote(value.to_s)
    acc.gsub("$#{index + 1}", literal)
  end
end

begin
  # --- the scratch database -------------------------------------------
  response['stage'] = 'connect'
  ActiveRecord::Base.establish_connection(request['admin_url'])
  admin = ActiveRecord::Base.connection
  name  = request['scratch_dbname']
  admin.execute(%(DROP DATABASE IF EXISTS "#{name}"))
  admin.execute(%(CREATE DATABASE "#{name}"))
  ActiveRecord::Base.remove_connection

  ActiveRecord::Base.establish_connection(request['scratch_url'])
  conn = ActiveRecord::Base.connection

  # Gems that add to the migration or schema DSL. Without them a migration
  # calling `safety_assured` raises NoMethodError, and an application whose
  # own schema.rb calls `create_trigger` or `create_view` cannot even be
  # loaded to build the pre-state. Each is optional: an app that does not
  # bundle one simply does not get it, which is why every require is
  # guarded rather than declared.
  loaded = []
  DSL_GEMS.each do |gem_name, installer|
    begin
      require gem_name
    rescue LoadError, StandardError
      next
    end
    loaded << gem_name
    # Some of them install themselves from a Railtie initializer, which
    # only runs when the application boots. Requiring the gem is then not
    # enough: `create_view` is added to the adapter by Scenic.load, and
    # without that call a schema.rb declaring a view raises NoMethodError
    # even though the gem is installed and loaded.
    next unless installer
    begin
      constant = Object.const_get(installer)
      constant.load if constant.respond_to?(:load)
    rescue StandardError
      next
    end
  end
  if defined?(StrongMigrations)
    # Its checks are a review, and reviewing is not what this is doing.
    # Left on, it would refuse to render exactly the migrations most worth
    # rendering.
    StrongMigrations.check_down = false if StrongMigrations.respond_to?(:check_down=)
    if StrongMigrations.respond_to?(:start_after=)
      StrongMigrations.start_after = 9_999_999_999_999_999
    end
  end
  response['gems_loaded'] = loaded
  response['strong_migrations'] = loaded.include?('strong_migrations')

  # --- the state the migration expects to find -------------------------
  # Nothing here is captured: the pre-state is not part of the change.
  # The stage is reported so the caller can tell a schema that would not
  # load from a migration that would not run: only the first is worth
  # retrying a different way.
  response['stage'] = 'schema'
  case request['schema_kind']
  when 'ruby'
    load request['schema_file']
  when 'sql'
    sql = File.read(request['schema_file'])
    # pg_dump output carries psql meta-commands, which are the client's, not
    # the server's, and are not executable over a connection.
    cleaned = sql.lines.reject { |l| l.start_with?(PSQL_META) }.join
    conn.execute(cleaned)
  end

  response['stage'] = 'replay'
  (request['replay'] || []).each do |path|
    before = ActiveRecord::Migration.descendants.dup
    load path
    klass = (ActiveRecord::Migration.descendants - before).find { |c| c.name && !c.name.start_with?('ActiveRecord::') }
    next unless klass
    m = klass.new
    m.verbose = false
    m.migrate(:up)
  end

  # --- the migration under assessment ----------------------------------
  response['stage'] = 'migration'
  captured = []
  subscriber = ActiveSupport::Notifications.subscribe('sql.active_record') do |*args|
    payload = args.last.is_a?(Hash) ? args.last : ActiveSupport::Notifications::Event.new(*args).payload
    captured << { 'sql' => payload[:sql], 'name' => payload[:name],
                  'binds' => (payload[:type_casted_binds] rescue nil) }
  end

  path = request['migration']
  # Rails' own filename grammar (ActiveRecord::Migration::MigrationFilenameRegexp,
  # used by MigrationContext#parse_migration_filename): version, name, and an
  # optional *scope*. The scope is what an engine's installed migrations carry
  # -- `..._create_active_storage_variant_records.active_storage.rb` -- and it
  # is not part of the class name. Splitting on the first underscore and
  # camelizing the rest would name that class
  # `CreateActiveStorageVariantRecords.activeStorage`, which does not exist.
  filename = File.basename(path)
  match = filename.match(/\A([0-9]+)_([_a-z0-9]*)\.?([_a-z0-9]*)?\.rb\z/)
  raise NameError, "#{filename} is not a Rails migration file name" unless match
  slug = match[2]
  class_name = slug.split('_').map { |w| w.empty? ? w : w[0].upcase + w[1..] }.join
  load path
  migration_class =
    begin
      Object.const_get(class_name)
    rescue NameError
      # A file whose class does not match its name is not something to
      # guess at: picking "some other migration class that appeared" is how
      # a verdict ends up describing the wrong change.
      raise NameError, "#{File.basename(path)} does not define #{class_name}"
    end

  migration = migration_class.new
  migration.verbose = false
  response['migration_class'] = class_name
  response['disable_ddl_transaction'] = !!migration.disable_ddl_transaction

  # Rails' own condition for wrapping a migration in a transaction
  # (Migrator#use_transaction?), reproduced so the captured BEGIN/COMMIT --
  # or their absence -- is what would really happen.
  if !migration.disable_ddl_transaction && conn.supports_ddl_transactions?
    conn.transaction { migration.migrate(:up) }
  else
    migration.migrate(:up)
  end

  ActiveSupport::Notifications.unsubscribe(subscriber)

  statements = captured.reject { |c| c['name'] == 'SCHEMA' }
                       .reject { |c| c['sql'].to_s =~ BOOKKEEPING }
                       .reject { |c| c['sql'].to_s =~ SETTING_READ }
                       .map { |c| { 'sql' => inline_binds(c['sql'], c['binds'], conn), 'name' => c['name'] } }

  response['statements'] = statements
  response['ok'] = true
rescue Exception => e   # rubocop:disable Lint/RescueException
  response['ok'] = false
  response['error'] = e.message.to_s.lines.first.to_s.strip
  response['error_class'] = e.class.name
ensure
  response['rails_version'] = (ActiveRecord::VERSION::STRING rescue nil)
  begin
    if request['drop_when_done']
      ActiveRecord::Base.remove_connection
      ActiveRecord::Base.establish_connection(request['admin_url'])
      ActiveRecord::Base.connection.execute(%(DROP DATABASE IF EXISTS "#{request['scratch_dbname']}"))
    end
  rescue StandardError
    # A leaked scratch database is untidy; failing the extraction over it
    # would be worse.
  end
  File.write(ARGV[1], JSON.generate(response))
end
