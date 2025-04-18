import os
from abc import ABC, abstractmethod
from typing import IO, Any, Callable, Dict, Union

import pandas as pd
import polars as pl
from pandas import DataFrame

from mage_ai.io.constants import SQL_RESERVED_WORDS
from mage_ai.shared.enum import StrEnum
from mage_ai.shared.logger import VerbosePrintHandler
from mage_ai.shared.models import BaseEnum
from mage_ai.shared.utils import clean_name

QUERY_ROW_LIMIT = 10_000_000


class DataSource(StrEnum):
    AIRTABLE = 'airtable'
    ALGOLIA = 'algolia'
    API = 'api'
    BIGQUERY = 'bigquery'
    CHROMA = 'chroma'
    CLICKHOUSE = 'clickhouse'
    DRUID = 'druid'
    DUCKDB = 'duckdb'
    FILE = 'file'
    GOOGLE_CLOUD_STORAGE = 'google_cloud_storage'
    GOOGLE_SHEETS = 'google_sheets'
    KAFKA = 'kafka'
    MYSQL = 'mysql'
    MSSQL = 'sqlserver'
    OPENSEARCH = 'opensearch'
    PINOT = 'pinot'
    POSTGRES = 'postgres'
    QDRANT = 'qdrant'
    REDSHIFT = 'redshift'
    S3 = 's3'
    SNOWFLAKE = 'snowflake'
    SPARK = 'spark'
    TRINO = 'trino'
    WEAVIATE = 'weaviate'


class FileFormat(StrEnum):
    CSV = 'csv'
    JSON = 'json'
    PARQUET = 'parquet'
    HDF5 = 'hdf5'
    XML = 'xml'
    EXCEL = 'excel'

    @classmethod
    def from_extension(cls, ext: str):
        if ext == 'csv':
            return cls.CSV
        elif ext == 'json':
            return cls.JSON
        elif ext == 'parquet':
            return cls.PARQUET
        elif ext == 'hdf5':
            return cls.HDF5
        elif ext == 'xml':
            return cls.XML
        elif ext in ('xls', 'xlsx'):
            return cls.EXCEL
        else:
            raise ValueError(
                f'None file format found for this file extension: {ext}'
            )


class ExportWritePolicy(BaseEnum):
    APPEND = 'append'
    FAIL = 'fail'
    REPLACE = 'replace'


class BaseIO(ABC):
    """
    Data connector interface. All data connectors must inherit from this interface.
    """

    def __init__(self, verbose=False) -> None:
        self.verbose = verbose
        self.printer = VerbosePrintHandler(f'{type(self).__name__} initialized', verbose=verbose)

    def _enforce_limit(self, query: str, limit: int = QUERY_ROW_LIMIT) -> str:
        """
        Modifies SQL SELECT query to enforce a limit on the number of rows returned by the query.
        This method is currently supports PostgreSQL syntax, which means it can be used with
        PostgreSQL, Amazon Redshift, Snowflake, and Google BigQuery.

        Args:
            query (str): The SQL query to modify
            limit (int): The limit on the number of rows to return.

        Returns:
            str: Modified query with limit on row count returned.
        """
        query = query.strip(';')

        return f"""
WITH subquery AS (
    {query}
)

SELECT *
FROM subquery
LIMIT {limit}
"""

    @abstractmethod
    def load(self, *args, **kwargs) -> DataFrame:
        """
        Loads a data frame from source, returns it to memory. Subclasses must
        override this method to specify how this data frame is to be returned.

        Returns:
            DataFrame: dataframe returned by the source.
        """
        pass

    @abstractmethod
    def export(self, df: DataFrame, *args, **kwargs) -> None:
        """
        Exports the input dataframe to the specified source. Subclasses must override
        this method to specify of this data frame should be exported.

        Args:
            df (DataFrame): Data frame to export.
        """
        pass


class BaseFile(BaseIO):
    """
    Data connector for file-like data sources (for example, loading from local
    filesystem or external file storages such as AWS S3)
    """

    @abstractmethod
    def exists(self, *args, **kwargs) -> bool:
        """
        Checks if content exists at the specified path.
        """
        pass

    def _get_file_format(self, filepath: Union[os.PathLike, str]) -> str:
        return FileFormat.from_extension(
            os.path.splitext(os.path.basename(filepath))[-1][1:]
        )

    def __get_reader(self, format: Union[FileFormat, str, None]) -> Callable:
        """
        Gets data frame reader based on file format

        Args:
            format (Union[FileFormat, str]): Format to get reader for.

        Raises:
            ValueError: Raised if invalid format specified.

        Returns:
            Callable: Returns the reader function that reads a dataframe from file
        """
        if format == FileFormat.CSV:
            return pd.read_csv
        elif format == FileFormat.JSON:
            return pd.read_json
        elif format == FileFormat.PARQUET:
            return pd.read_parquet
        elif format == FileFormat.HDF5:
            return pd.read_hdf
        elif format == FileFormat.XML:
            return pd.read_xml
        elif format == FileFormat.EXCEL:
            return pd.read_excel
        else:
            raise ValueError(f"Invalid format '{format}' specified.")

    def _read(
        self,
        input: Union[IO, os.PathLike, str],
        format: Union[FileFormat, str, None],
        limit: int = QUERY_ROW_LIMIT,
        **kwargs,
    ) -> DataFrame:
        """
        Loads the data frame from the filepath or buffer specified.

        Args:
            input (Union[IO, os.PathLike]): Input buffer to read dataframe from.
            Can be a stream or a filepath.
            format (Union[FileFormat, str]): Format of the data frame as stored
            in stream or filepath.

        Returns:
            DataFrame: Data frame object loaded from the specified data frame.
        """
        reader = self.__get_reader(format)
        df = reader(input, **kwargs)
        return df

    def __trim_df(self, df: DataFrame, limit: int = QUERY_ROW_LIMIT) -> DataFrame:
        """
        Truncates data frame to `limit` rows

        Args:
            df (DataFrame): Data frame to truncate out.

        Returns:
            DataFrame: Truncated data frame with removed rows.
        """
        return df[:limit]

    def _write(
        self,
        df,
        format: Union[FileFormat, str, None],
        output: Union[IO, os.PathLike, str],
        **kwargs,
    ) -> None:
        """
        Base method for writing a data frame to some buffer or file.

        Two caveats if the format is HDF5:
        - Data frames can only be written to files, not to buffers
        - The default key under which the data frame is stored is the
          stem of the filename. For example, if the file to write the HDF5 file
          to is 'storage/my_dataframe.hdf5', the key would be 'my_dataframe'. This
          can be overridden using the `key` keyword argument.

        Args:
            df (DataFrame): Data frame to write.
            format (Union[FileFormat, str]): Format to write the data frame as.
            output (Union[IO, os.PathLike]): Output stream/filepath to write data frame to.
        """
        if format is None:
            format = FileFormat.PARQUET
        writer = self.__get_writer(df, format)
        if format == FileFormat.HDF5:
            if isinstance(output, IO):
                raise ValueError('Cannot write HDF5 file to buffer of any type.')
            name = os.path.splitext(os.path.basename(output))[0]
            kwargs.setdefault('key', name)
        elif format == FileFormat.PARQUET and isinstance(df, DataFrame):
            if 'coerce_timestamps' not in kwargs:
                kwargs['coerce_timestamps'] = 'ms'
                kwargs['allow_truncated_timestamps'] = True
        writer(output, **kwargs)

    def __get_writer(
        self,
        df,
        format: Union[FileFormat, str, None],
    ) -> Callable:
        """
        Fetches the appropriate file writer based on format

        Args:
            df (DataFrame): Data frame to get file writer for.
            format (Union[FileFormat, str]): Format to write the data frame as.

        Returns:
            Callable: File writer method
        """
        if isinstance(df, pl.DataFrame):  # polars DataFrame
            if format == FileFormat.CSV:
                return df.write_csv
            elif format == FileFormat.JSON:
                return df.write_json
            elif format == FileFormat.HDF5:
                return df.write_hdf5
            elif format == FileFormat.XML:
                return df.write_xml
            elif format == FileFormat.EXCEL:
                return df.write_excel
            return df.write_parquet

        elif isinstance(df, DataFrame):  # pandas DataFrame
            if format == FileFormat.CSV:
                return df.to_csv
            elif format == FileFormat.JSON:
                return df.to_json
            elif format == FileFormat.HDF5:
                return df.to_hdf
            elif format == FileFormat.XML:
                return df.to_xml
            elif format == FileFormat.EXCEL:
                return df.to_excel
            return df.to_parquet

    def __del__(self):
        if self.verbose and self.printer.exists_previous_message:
            print('')


class BaseSQLDatabase(BaseIO):
    """
    Base data connector for connecting to a SQL database. This adds `query` method which allows
    a user to send queries to the database server.
    """

    @abstractmethod
    def execute(self, query_string: str, **kwargs) -> None:
        """
        Sends query to the connected database

        Args:
            query_string (str): Query to send to the connected database.
            **kwargs: Additional arguments to pass to query, such as query configurations
        """
        pass

    def sample(self, schema: str, table: str, size: int = QUERY_ROW_LIMIT, **kwargs) -> DataFrame:
        """
        Sample data from a table in the connected database. Sample is not
        guaranteed to be random.

        Args:
            schema (str): The schema to select the table from.
            size (int): The number of rows to sample. Defaults to 10,000,000.
            table (str): The table to sample from in the connected database.

        Returns:
            DataFrame: Sampled data from the data frame.
        """
        return self.load(f'SELECT * FROM {schema}.{table} LIMIT {str(size)};', **kwargs)

    def _clean_query(self, query_string: str) -> str:
        """
        Cleans query before sending to database. Cleaning steps include:
        - Removing surrounding whitespace, newlines, and tabs

        Args:
            query_string (str): Query string to clean

        Returns:
            str: Clean query string
        """
        return query_string.strip(' \n\t')

    def _clean_column_name(
        self,
        column_name: str,
        allow_reserved_words: bool = False,
        auto_clean_name: bool = True,
        case_sensitive: bool = False,
    ) -> str:
        if not auto_clean_name:
            return column_name
        col_new = clean_name(column_name, case_sensitive=case_sensitive)
        if not allow_reserved_words and col_new.upper() in SQL_RESERVED_WORDS:
            col_new = f'_{col_new}'
        return col_new

    def execute_query_raw(self, query: str, configuration: Dict = None, **kwargs) -> None:
        pass


class BaseSQLConnection(BaseSQLDatabase):
    """
    Data connector for connected SQL data sources. Can be used as a context manager or by
    manually opening or closing the connection to the SQL data source after data loading
    is complete.

    WARNING: queries may continue to run on data source unless connection manually closed.
    For this reason it is recommended to use a context
    manager when connecting to external data sources.
    """

    def __init__(self, verbose: bool = False, **kwargs) -> None:
        """
        Initializes the connection with the settings given as keyword arguments.
        Specific data connectors will have access to different settings.
        """
        super().__init__(verbose=verbose)
        self.settings = kwargs

    def close(self) -> None:
        """
        Close the underlying connection to the SQL data source if open. Else will do nothing.
        """
        if '_ctx' in self.__dict__:
            self._ctx.close()
            del self._ctx
        if self.verbose and self.printer.exists_previous_message:
            print('')

    def commit(self) -> None:
        """
        Commits all changes made to database since last commit
        """
        self.conn.commit()

    @property
    def conn(self) -> Any:
        """
        Returns the connection object to the SQL data source. The exact connection type depends
        on the source and the definition of the data connector.
        """
        try:
            return self._ctx
        except AttributeError:
            raise ConnectionError(
                'No connection currently open. Open a new connection to access this property.'
            )

    @abstractmethod
    def open(self) -> None:
        """
        Opens an underlying connection to the SQL data source.
        """
        pass

    def rollback(self) -> None:
        """
        Rolls back (deletes) all changes made to database since last commit.
        """
        self.conn.rollback()

    def __del__(self):
        self.close()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()
