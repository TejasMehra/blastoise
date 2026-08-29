# frozen_string_literal: true
#
# Validation helper, not part of the shipped tool.
#
# Builds a database in the pre-migration state and stops there. The
# extraction validator needs two databases prepared identically: one it
# runs the real migration against, one it applies the extracted SQL to.
# harness.rb builds the first; this builds the second.

require 'json'

# Same stdlib the harness supplies, for the same reason: gems that assume a
# booted Rails already required them (activesupport's ::Logger, scenic's
# ::TSort).
%w[logger tsort set singleton benchmark].each do |lib|
  begin
    require lib
  rescue LoadError
    next
  end
end

require 'active_record'

# The same DSL gems the shipped harness loads, installed the same way --
# including the `.load` call that a Railtie would normally make. Database B
# has to be built from the schema file exactly as database A was, or a
# schema.rb calling `create_view` loads on one side and not the other, and
# the comparison measures the setup rather than the SQL.
{
  'strong_migrations' => nil,
  'hairtrigger'       => nil,
  'neighbor'          => nil,
  'fx'                => 'Fx',
  'scenic'            => 'Scenic',
}.each do |gem_name, installer|
  begin
    require gem_name
  rescue LoadError, StandardError
    next
  end
  next unless installer
  begin
    constant = Object.const_get(installer)
    constant.load if constant.respond_to?(:load)
  rescue StandardError
    next
  end
end

request  = JSON.parse(File.read(ARGV[0]))
response = { 'ok' => false }

PSQL_META = 92.chr

begin
  ActiveRecord::Base.establish_connection(request['admin_url'])
  admin = ActiveRecord::Base.connection
  name  = request['scratch_dbname']
  admin.execute(%(DROP DATABASE IF EXISTS "#{name}"))
  admin.execute(%(CREATE DATABASE "#{name}"))
  ActiveRecord::Base.remove_connection

  ActiveRecord::Base.establish_connection(request['scratch_url'])
  conn = ActiveRecord::Base.connection

  case request['schema_kind']
  when 'ruby'
    load request['schema_file']
  when 'sql'
    sql = File.read(request['schema_file'])
    conn.execute(sql.lines.reject { |l| l.start_with?(PSQL_META) }.join)
  end

  # The same preceding migrations the harness ran against database A. Both
  # databases have to start from the same state or the comparison is
  # measuring the difference in their setup rather than in the SQL.
  (request['replay'] || []).each do |path|
    before = ActiveRecord::Migration.descendants.dup
    load path
    klass = (ActiveRecord::Migration.descendants - before).find do |c|
      c.name && !c.name.start_with?('ActiveRecord::')
    end
    next unless klass
    m = klass.new
    m.verbose = false
    m.migrate(:up)
  end

  response['ok'] = true
rescue Exception => e # rubocop:disable Lint/RescueException
  response['error'] = e.message.to_s.lines.first.to_s.strip
  response['error_class'] = e.class.name
ensure
  File.write(ARGV[1], JSON.generate(response))
end
