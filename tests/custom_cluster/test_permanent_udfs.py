# Copyright (c) 2015 Cloudera, Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import os
import pytest
import subprocess
from tests.beeswax.impala_beeswax import ImpalaBeeswaxException
from tests.common.custom_cluster_test_suite import CustomClusterTestSuite
from tests.util.filesystem_utils import get_fs_path

class TestUdfPersistence(CustomClusterTestSuite):
  """ Tests the behavior of UDFs and UDAs between catalog restarts. With IMPALA-1748, these
  functions are persisted to the metastore and are loaded again during catalog startup"""

  DATABASE = 'udf_permanent_test'
  JAVA_FN_TEST_DB = 'java_permanent_test'
  HIVE_IMPALA_INTEGRATION_DB = 'hive_impala_integration_db'
  HIVE_UDF_JAR = os.getenv('DEFAULT_FS') + '/test-warehouse/hive-exec.jar';
  JAVA_UDF_JAR = os.getenv('DEFAULT_FS') + '/test-warehouse/impala-hive-udfs.jar';

  @classmethod
  def get_workload(cls):
    return 'functional-query'

  @classmethod
  def add_test_dimensions(cls):
    super(TestUdfPersistence, cls).add_test_dimensions()
    cls.TestMatrix.add_dimension(create_uncompressed_text_dimension(cls.get_workload()))

  def setup_method(self, method):
    super(TestUdfPersistence, self).setup_method(method)
    impalad = self.cluster.impalads[0]
    self.client = impalad.service.create_beeswax_client()
    self.__cleanup()
    self.__load_drop_functions(
        self.CREATE_UDFS_TEMPLATE, self.DATABASE,
        get_fs_path('/test-warehouse/libTestUdfs.so'))
    self.__load_drop_functions(
        self.DROP_SAMPLE_UDAS_TEMPLATE, self.DATABASE,
        get_fs_path('/test-warehouse/libudasample.so'))
    self.__load_drop_functions(
        self.CREATE_SAMPLE_UDAS_TEMPLATE, self.DATABASE,
        get_fs_path('/test-warehouse/libudasample.so'))
    self.__load_drop_functions(
        self.CREATE_TEST_UDAS_TEMPLATE, self.DATABASE,
        get_fs_path('/test-warehouse/libTestUdas.so'))
    self.uda_count =\
        self.CREATE_SAMPLE_UDAS_TEMPLATE.count("create aggregate function") +\
        self.CREATE_TEST_UDAS_TEMPLATE.count("create aggregate function")
    self.udf_count = self.CREATE_UDFS_TEMPLATE.count("create function")
    self.client.execute("CREATE DATABASE IF NOT EXISTS %s" % self.JAVA_FN_TEST_DB)
    self.client.execute("CREATE DATABASE IF NOT EXISTS %s" %
        self.HIVE_IMPALA_INTEGRATION_DB)

  def teardown_method(self, method):
    self.__cleanup()

  def __cleanup(self):
    self.client.execute("DROP DATABASE IF EXISTS %s CASCADE" % self.DATABASE)
    self.client.execute("DROP DATABASE IF EXISTS %s CASCADE" % self.JAVA_FN_TEST_DB)
    self.client.execute("DROP DATABASE IF EXISTS %s CASCADE"
       % self.HIVE_IMPALA_INTEGRATION_DB)

  def run_stmt_in_hive(self, stmt):
    """
    Run a statement in Hive, returning stdout if successful and throwing
    RuntimeError(stderr) if not.
    """
    call = subprocess.Popen(
        ['hive', '-e', stmt], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (stdout, stderr) = call.communicate()
    call.wait()
    if call.returncode != 0:
      raise RuntimeError(stderr)
    return stdout

  def __load_drop_functions(self, template, database, location):
    queries = template.format(database=database, location=location)
    # Split queries and remove empty lines
    queries = [q for q in queries.split(';') if q.strip()]
    for query in queries:
      result = self.client.execute(query)
      assert result is not None

  def __restart_cluster(self):
    self._stop_impala_cluster()
    self._start_impala_cluster(list())
    impalad = self.cluster.impalads[0]
    self.client = impalad.service.create_beeswax_client()

  def verify_function_count(self, query, count):
    result = self.client.execute(query)
    assert result is not None and len(result.data) == count

  @pytest.mark.execute_serially
  def test_permanent_udfs(self):
    # Make sure the pre-calculated count tallies with the number of
    # functions shown using "show [aggregate] functions" statement
    self.verify_function_count(
            "SHOW FUNCTIONS in {0}".format(self.DATABASE), self.udf_count);
    self.verify_function_count(
            "SHOW AGGREGATE FUNCTIONS in {0}".format(self.DATABASE), self.uda_count)
    # invalidate metadata and make sure the count tallies
    result = self.client.execute("INVALIDATE METADATA")
    self.verify_function_count(
            "SHOW FUNCTIONS in {0}".format(self.DATABASE), self.udf_count);
    self.verify_function_count(
            "SHOW AGGREGATE FUNCTIONS in {0}".format(self.DATABASE), self.uda_count)
    # Restart the cluster, this triggers a full metadata reload
    self.__restart_cluster()
    # Make sure the counts of udfs and udas match post restart
    self.verify_function_count(
            "SHOW FUNCTIONS in {0}".format(self.DATABASE), self.udf_count);
    self.verify_function_count(
            "SHOW AGGREGATE FUNCTIONS in {0}".format(self.DATABASE), self.uda_count)
    # Drop sample udas and verify the count matches pre and post restart
    self.__load_drop_functions(
        self.DROP_SAMPLE_UDAS_TEMPLATE, self.DATABASE,
        get_fs_path('/test-warehouse/libudasample.so'))
    self.verify_function_count(
            "SHOW AGGREGATE FUNCTIONS in {0}".format(self.DATABASE), 1)
    self.__restart_cluster()
    self.verify_function_count(
            "SHOW AGGREGATE FUNCTIONS in {0}".format(self.DATABASE), 1)


  def __verify_udf_in_hive(self, udf):
    (query, result) = self.SAMPLE_JAVA_UDFS_TEST[udf]
    stdout = self.run_stmt_in_hive("select " + query.format(
        db=self.HIVE_IMPALA_INTEGRATION_DB))
    assert stdout is not None and result in str(stdout)

  def __verify_udf_in_impala(self, udf):
    (query, result) = self.SAMPLE_JAVA_UDFS_TEST[udf]
    stdout = self.client.execute("select " + query.format(
        db=self.HIVE_IMPALA_INTEGRATION_DB))
    assert stdout is not None and result in str(stdout.data)

  @pytest.mark.execute_serially
  def test_java_udfs_hive_integration(self):
    ''' This test checks the integration between Hive and Impala on
    CREATE FUNCTION and DROP FUNCTION statements for persistent Java UDFs.
    The main objective of the test is to check the following four cases.
      - Add Java UDFs from Impala and make sure they are visible in Hive
      - Drop Java UDFs from Impala and make sure this reflects in Hive.
      - Add Java UDFs from Hive and make sure they are visitble in Impala
      - Drop Java UDFs from Hive and make sure this reflects in Impala
    '''
    # Add Java UDFs from Impala and check if they are visible in Hive.
    # Hive has bug that doesn't display the permanent function in show functions
    # statement. So this test relies on describe function statement which prints
    # a message if the function is not present.
    for (fn, fn_symbol) in self.SAMPLE_JAVA_UDFS:
      self.client.execute(self.DROP_JAVA_UDF_TEMPLATE.format(
          db=self.HIVE_IMPALA_INTEGRATION_DB, function=fn))
      self.client.execute(self.CREATE_JAVA_UDF_TEMPLATE.format(
          db=self.HIVE_IMPALA_INTEGRATION_DB, function=fn,
          location=self.HIVE_UDF_JAR, symbol=fn_symbol))
      hive_stdout = self.run_stmt_in_hive("DESCRIBE FUNCTION %s.%s"
        % (self.HIVE_IMPALA_INTEGRATION_DB, fn))
      assert "does not exist" not in hive_stdout
      self.__verify_udf_in_hive(fn)
      # Drop the function from Impala and check if it reflects in Hive.
      self.client.execute(self.DROP_JAVA_UDF_TEMPLATE.format(
          db=self.HIVE_IMPALA_INTEGRATION_DB, function=fn))
      hive_stdout = self.run_stmt_in_hive("DESCRIBE FUNCTION %s.%s"
        % (self.HIVE_IMPALA_INTEGRATION_DB, fn))
      assert "does not exist" in hive_stdout

    # Create the same set of functions from Hive and make sure they are visible
    # in Impala.
    for (fn, fn_symbol) in self.SAMPLE_JAVA_UDFS:
      self.run_stmt_in_hive(self.CREATE_HIVE_UDF_TEMPLATE.format(
          db=self.HIVE_IMPALA_INTEGRATION_DB, function=fn,
          location=self.HIVE_UDF_JAR, symbol=fn_symbol))
    self.client.execute("INVALIDATE METADATA")
    for (fn, fn_symbol) in self.SAMPLE_JAVA_UDFS:
      result = self.client.execute("SHOW FUNCTIONS IN %s" %
          self.HIVE_IMPALA_INTEGRATION_DB)
      assert result is not None and len(result.data) > 0 and\
          fn in str(result.data)
      self.__verify_udf_in_impala(fn)
      # Drop the function in Hive and make sure it reflects in Impala.
      self.run_stmt_in_hive(self.DROP_JAVA_UDF_TEMPLATE.format(
          db=self.HIVE_IMPALA_INTEGRATION_DB, function=fn))
    self.client.execute("INVALIDATE METADATA")
    self.verify_function_count(
            "SHOW FUNCTIONS in {0}".format(self.HIVE_IMPALA_INTEGRATION_DB), 0)

  @pytest.mark.execute_serially
  def test_java_udfs_from_impala(self):
    """ This tests checks the behavior of permanent Java UDFs in Impala."""
    self.verify_function_count(
            "SHOW FUNCTIONS in {0}".format(self.JAVA_FN_TEST_DB), 0);
    # Create a non persistent Java UDF and make sure we can't create a
    # persistent Java UDF with same name
    self.client.execute("create function %s.%s(boolean) returns boolean "\
        "location '%s' symbol='%s'" % (self.JAVA_FN_TEST_DB, "identity",
        self.JAVA_UDF_JAR, "com.cloudera.impala.TestUdf"))
    result = self.execute_query_expect_failure(self.client,
        self.CREATE_JAVA_UDF_TEMPLATE.format(db=self.JAVA_FN_TEST_DB,
        function="identity", location=self.JAVA_UDF_JAR,
        symbol="com.cloudera.impala.TestUdf"))
    assert "Function already exists" in str(result)
    # Test the same with a NATIVE function
    self.client.execute("create function {database}.identity(int) "\
        "returns int location '{location}' symbol='Identity'".format(
        database=self.JAVA_FN_TEST_DB,
        location="/test-warehouse/libTestUdfs.so"))
    result = self.execute_query_expect_failure(self.client,
        self.CREATE_JAVA_UDF_TEMPLATE.format(db=self.JAVA_FN_TEST_DB,
        function="identity", location=self.JAVA_UDF_JAR,
        symbol="com.cloudera.impala.TestUdf"))
    assert "Function already exists" in str(result)

    # Test the reverse. Add a persistent Java UDF and ensure we cannot
    # add non persistent Java UDFs or NATIVE functions with the same name.
    self.client.execute(self.CREATE_JAVA_UDF_TEMPLATE.format(
        db=self.JAVA_FN_TEST_DB, function="identity_java",
        location=self.JAVA_UDF_JAR, symbol="com.cloudera.impala.TestUdf"))
    result = self.execute_query_expect_failure(self.client, "create function "\
        "%s.%s(boolean) returns boolean location '%s' symbol='%s'" % (
        self.JAVA_FN_TEST_DB, "identity_java", self.JAVA_UDF_JAR,
        "com.cloudera.impala.TestUdf"))
    assert "Function already exists" in str(result)
    result = self.execute_query_expect_failure(self.client, "create function "\
        "{database}.identity_java(int) returns int location '{location}' "\
        "symbol='Identity'".format(database=self.JAVA_FN_TEST_DB,
        location="/test-warehouse/libTestUdfs.so"))
    assert "Function already exists" in str(result)
    # With IF NOT EXISTS, the query shouldn't fail.
    result = self.execute_query_expect_success(self.client, "create function "\
        " if not exists {database}.identity_java(int) returns int location "\
        "'{location}' symbol='Identity'".format(database=self.JAVA_FN_TEST_DB,
        location="/test-warehouse/libTestUdfs.so"))
    result = self.client.execute("SHOW FUNCTIONS in %s" % self.JAVA_FN_TEST_DB)
    self.execute_query_expect_success(self.client,
        "DROP FUNCTION IF EXISTS {db}.impala_java".format(db=self.JAVA_FN_TEST_DB))

    # Drop the persistent Java function.
    # Test the same create with IF NOT EXISTS. No exception should be thrown.
    # Add a Java udf which has a few incompatible 'evaluate' functions in the
    # symbol class. Catalog should load only the compatible ones. JavaUdfTest
    # has 8 evaluate signatures out of which only 3 are valid.
    compatibility_fn_count = 3
    self.client.execute(self.CREATE_JAVA_UDF_TEMPLATE.format(
        db=self.JAVA_FN_TEST_DB, function="compatibility",
        location=self.JAVA_UDF_JAR, symbol="com.cloudera.impala.JavaUdfTest"))
    self.verify_function_count(
        "SHOW FUNCTIONS IN %s like 'compatibility*'" % self.JAVA_FN_TEST_DB,
        compatibility_fn_count)
    result = self.client.execute("SHOW FUNCTIONS in %s" % self.JAVA_FN_TEST_DB)
    function_count = len(result.data)
    # Invalidating metadata should preserve all the functions
    self.client.execute("INVALIDATE METADATA")
    self.verify_function_count(
        "SHOW FUNCTIONS IN %s" % self.JAVA_FN_TEST_DB, function_count)
    # Restarting the cluster should preserve only the persisted functions. In
    # this case, identity(boolean) should be wiped out.
    self.__restart_cluster()
    self.verify_function_count(
        "SHOW FUNCTIONS IN %s" % self.JAVA_FN_TEST_DB, function_count-1)
    # Dropping persisted Java UDFs with old syntax should raise an exception
    self.execute_query_expect_failure(self.client,
        "DROP FUNCTION compatibility(smallint)")
    self.verify_function_count(
        "SHOW FUNCTIONS IN %s like 'compatibility*'" % self.JAVA_FN_TEST_DB, 3)
    # Drop the functions and make sure they don't appear post restart.
    self.client.execute("DROP FUNCTION %s.compatibility" % self.JAVA_FN_TEST_DB)
    self.verify_function_count(
        "SHOW FUNCTIONS IN %s like 'compatibility*'" % self.JAVA_FN_TEST_DB, 0)
    self.__restart_cluster()
    self.verify_function_count(
        "SHOW FUNCTIONS IN %s like 'compatibility*'" % self.JAVA_FN_TEST_DB, 0)

    # Try to load a UDF that has no compatible signatures. Make sure it is not added
    # to Hive and Impala.
    result = self.execute_query_expect_failure(self.client,
        self.CREATE_JAVA_UDF_TEMPLATE.format(db=self.JAVA_FN_TEST_DB, function="badudf",
        location=self.JAVA_UDF_JAR, symbol="com.cloudera.impala.IncompatibleUdfTest"))
    assert "No compatible function signatures" in str(result)
    self.verify_function_count(
        "SHOW FUNCTIONS IN %s like 'badudf*'" % self.JAVA_FN_TEST_DB, 0)
    result = self.run_stmt_in_hive("DESCRIBE FUNCTION %s.%s"
        % (self.JAVA_FN_TEST_DB, "badudf"))
    assert "does not exist" in str(result)
    # Create the same function from hive and make sure Impala doesn't load any signatures.
    self.run_stmt_in_hive(self.CREATE_HIVE_UDF_TEMPLATE.format(
        db=self.JAVA_FN_TEST_DB, function="badudf",
        location=self.JAVA_UDF_JAR, symbol="com.cloudera.impala.IncompatibleUdfTest"))
    result = self.run_stmt_in_hive("DESCRIBE FUNCTION %s.%s"
        % (self.JAVA_FN_TEST_DB, "badudf"))
    assert "does not exist" not in str(result)
    self.client.execute("INVALIDATE METADATA")
    self.verify_function_count(
        "SHOW FUNCTIONS IN %s like 'badudf*'" % self.JAVA_FN_TEST_DB, 0)
    # Add a function with the same name from Impala. It should fail.
    result = self.execute_query_expect_failure(self.client,
        self.CREATE_JAVA_UDF_TEMPLATE.format(db=self.JAVA_FN_TEST_DB, function="badudf",
        location=self.JAVA_UDF_JAR, symbol="com.cloudera.impala.TestUdf"))
    assert "Function badudf already exists" in str(result)
    # Drop the function and make sure the function if dropped from hive
    self.client.execute(self.DROP_JAVA_UDF_TEMPLATE.format(
        db=self.JAVA_FN_TEST_DB, function="badudf"))
    result = self.run_stmt_in_hive("DESCRIBE FUNCTION %s.%s"
        % (self.JAVA_FN_TEST_DB, "badudf"))
    assert "does not exist" in str(result)

  # Create sample UDA functions in {database} from library {location}

  DROP_SAMPLE_UDAS_TEMPLATE = """
    drop function if exists {database}.test_count(int);
    drop function if exists {database}.hll(int);
    drop function if exists {database}.sum_small_decimal(decimal(9,2));
  """

  CREATE_JAVA_UDF_TEMPLATE = """
    CREATE FUNCTION {db}.{function} LOCATION '{location}' symbol='{symbol}'
  """

  CREATE_HIVE_UDF_TEMPLATE = """
    CREATE FUNCTION {db}.{function} as '{symbol}' USING JAR '{location}'
  """

  DROP_JAVA_UDF_TEMPLATE = "DROP FUNCTION IF EXISTS {db}.{function}"

  # Sample java udfs from hive-exec.jar. Function name to symbol class mapping
  SAMPLE_JAVA_UDFS = [
      ('udfpi', 'org.apache.hadoop.hive.ql.udf.UDFPI'),
      ('udfbin', 'org.apache.hadoop.hive.ql.udf.UDFBin'),
      ('udfhex', 'org.apache.hadoop.hive.ql.udf.UDFHex'),
      ('udfconv', 'org.apache.hadoop.hive.ql.udf.UDFConv'),
      ('udfhour', 'org.apache.hadoop.hive.ql.udf.UDFHour'),
      ('udflike', 'org.apache.hadoop.hive.ql.udf.UDFLike'),
      ('udfsign', 'org.apache.hadoop.hive.ql.udf.UDFSign'),
      ('udfyear', 'org.apache.hadoop.hive.ql.udf.UDFYear'),
      ('udfascii','org.apache.hadoop.hive.ql.udf.UDFAscii')
  ]

  # Simple tests to verify java udfs in SAMPLE_JAVA_UDFS
  SAMPLE_JAVA_UDFS_TEST = {
    'udfpi' : ('{db}.udfpi()', '3.141592653589793'),
    'udfbin' : ('{db}.udfbin(123)', '1111011'),
    'udfhex' : ('{db}.udfhex(123)', '7B'),
    'udfconv'  : ('{db}.udfconv("100", 2, 10)', '4'),
    'udfhour'  : ('{db}.udfhour("12:55:12")', '12'),
    'udflike'  : ('{db}.udflike("abc", "def")', 'false'),
    'udfsign'  : ('{db}.udfsign(0)', '0'),
    'udfyear' : ('{db}.udfyear("1990-02-06")', '1990'),
    'udfascii' : ('{db}.udfascii("abc")','97')
  }

  CREATE_SAMPLE_UDAS_TEMPLATE = """
    create database if not exists {database};

    create aggregate function {database}.test_count(int) returns bigint
    location '{location}' update_fn='CountUpdate';

    create aggregate function {database}.hll(int) returns string
    location '{location}' update_fn='HllUpdate';

    create aggregate function {database}.sum_small_decimal(decimal(9,2))
    returns decimal(9,2) location '{location}' update_fn='SumSmallDecimalUpdate';
  """

  # Create test UDA functions in {database} from library {location}
  CREATE_TEST_UDAS_TEMPLATE = """
    drop function if exists {database}.trunc_sum(double);

    create database if not exists {database};

    create aggregate function {database}.trunc_sum(double)
    returns bigint intermediate double location '{location}'
    update_fn='TruncSumUpdate' merge_fn='TruncSumMerge'
    serialize_fn='TruncSumSerialize' finalize_fn='TruncSumFinalize';
  """

  # Create test UDF functions in {database} from library {location}
  CREATE_UDFS_TEMPLATE = """
    drop function if exists {database}.identity(boolean);
    drop function if exists {database}.identity(tinyint);
    drop function if exists {database}.identity(smallint);
    drop function if exists {database}.identity(int);
    drop function if exists {database}.identity(bigint);
    drop function if exists {database}.identity(float);
    drop function if exists {database}.identity(double);
    drop function if exists {database}.identity(string);
    drop function if exists {database}.identity(timestamp);
    drop function if exists {database}.identity(decimal(9,0));
    drop function if exists {database}.identity(decimal(18,1));
    drop function if exists {database}.identity(decimal(38,10));
    drop function if exists {database}.all_types_fn(
        string, boolean, tinyint, smallint, int, bigint, float, double, decimal(2,0));
    drop function if exists {database}.no_args();
    drop function if exists {database}.var_and(boolean...);
    drop function if exists {database}.var_sum(int...);
    drop function if exists {database}.var_sum(double...);
    drop function if exists {database}.var_sum(string...);
    drop function if exists {database}.var_sum(decimal(4,2)...);
    drop function if exists {database}.var_sum_multiply(double, int...);
    drop function if exists {database}.constant_timestamp();
    drop function if exists {database}.validate_arg_type(string);
    drop function if exists {database}.count_rows();
    drop function if exists {database}.constant_arg(int);
    drop function if exists {database}.validate_open(int);
    drop function if exists {database}.mem_test(bigint);
    drop function if exists {database}.mem_test_leaks(bigint);
    drop function if exists {database}.unmangled_symbol();
    drop function if exists {database}.four_args(int, int, int, int);
    drop function if exists {database}.five_args(int, int, int, int, int);
    drop function if exists {database}.six_args(int, int, int, int, int, int);
    drop function if exists {database}.seven_args(int, int, int, int, int, int, int);
    drop function if exists {database}.eight_args(int, int, int, int, int, int, int, int);

    create database if not exists {database};

    create function {database}.identity(boolean) returns boolean
    location '{location}' symbol='Identity';

    create function {database}.identity(tinyint) returns tinyint
    location '{location}' symbol='Identity';

    create function {database}.identity(smallint) returns smallint
    location '{location}' symbol='Identity';

    create function {database}.identity(int) returns int
    location '{location}' symbol='Identity';

    create function {database}.identity(bigint) returns bigint
    location '{location}' symbol='Identity';

    create function {database}.identity(float) returns float
    location '{location}' symbol='Identity';

    create function {database}.identity(double) returns double
    location '{location}' symbol='Identity';

    create function {database}.identity(string) returns string
    location '{location}'
    symbol='_Z8IdentityPN10impala_udf15FunctionContextERKNS_9StringValE';

    create function {database}.identity(timestamp) returns timestamp
    location '{location}'
    symbol='_Z8IdentityPN10impala_udf15FunctionContextERKNS_12TimestampValE';

    create function {database}.identity(decimal(9,0)) returns decimal(9,0)
    location '{location}'
    symbol='_Z8IdentityPN10impala_udf15FunctionContextERKNS_10DecimalValE';

    create function {database}.identity(decimal(18,1)) returns decimal(18,1)
    location '{location}'
    symbol='_Z8IdentityPN10impala_udf15FunctionContextERKNS_10DecimalValE';

    create function {database}.identity(decimal(38,10)) returns decimal(38,10)
    location '{location}'
    symbol='_Z8IdentityPN10impala_udf15FunctionContextERKNS_10DecimalValE';

    create function {database}.all_types_fn(
        string, boolean, tinyint, smallint, int, bigint, float, double, decimal(2,0))
    returns int
    location '{location}' symbol='AllTypes';

    create function {database}.no_args() returns string
    location '{location}'
    symbol='_Z6NoArgsPN10impala_udf15FunctionContextE';

    create function {database}.var_and(boolean...) returns boolean
    location '{location}' symbol='VarAnd';

    create function {database}.var_sum(int...) returns int
    location '{location}' symbol='VarSum';

    create function {database}.var_sum(double...) returns double
    location '{location}' symbol='VarSum';

    create function {database}.var_sum(string...) returns int
    location '{location}' symbol='VarSum';

    create function {database}.var_sum(decimal(4,2)...) returns decimal(18,2)
    location '{location}' symbol='VarSum';

    create function {database}.var_sum_multiply(double, int...) returns double
    location '{location}'
    symbol='_Z14VarSumMultiplyPN10impala_udf15FunctionContextERKNS_9DoubleValEiPKNS_6IntValE';

    create function {database}.constant_timestamp() returns timestamp
    location '{location}' symbol='ConstantTimestamp';

    create function {database}.validate_arg_type(string) returns boolean
    location '{location}' symbol='ValidateArgType';

    create function {database}.count_rows() returns bigint
    location '{location}' symbol='Count' prepare_fn='CountPrepare' close_fn='CountClose';

    create function {database}.constant_arg(int) returns int
    location '{location}' symbol='ConstantArg' prepare_fn='ConstantArgPrepare' close_fn='ConstantArgClose';

    create function {database}.validate_open(int) returns boolean
    location '{location}' symbol='ValidateOpen'
    prepare_fn='ValidateOpenPrepare' close_fn='ValidateOpenClose';

    create function {database}.mem_test(bigint) returns bigint
    location '{location}' symbol='MemTest'
    prepare_fn='MemTestPrepare' close_fn='MemTestClose';

    create function {database}.mem_test_leaks(bigint) returns bigint
    location '{location}' symbol='MemTest'
    prepare_fn='MemTestPrepare';
  """