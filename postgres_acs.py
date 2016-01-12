import os
import csv
import sys
import urllib2
import argparse
from copy import deepcopy
from zipfile import ZipFile
from os.path import dirname, exists, join, realpath

import xlrd
from sqlalchemy import create_engine, MetaData, Table, Column, Numeric, Text

# geography groupings offered by the Census Bureau
ACS_GEOGRAPHY = [
    'Tracts_Block_Groups_Only',
    'All_Geographies_Not_Tracts_Block_Groups'
]
ACS_PRIMARY_KEY = [
    'STUSAB',
    'LOGRECNO'
]


def get_states_mapping(value_type):
    """Maps state abbreviations to their full name or FIPS code"""

    value_dict = {'name': 'State', 'fips': 'FIPS Code'}
    try:
        value_field = value_dict[value_type]
    except KeyError:
        print 'Invalid value type supplied for states mapping'
        print 'options are: "name" and "fips"'
        exit()

    states = dict()
    states_csv_path = join(realpath('.'), 'census_states.csv')
    with open(states_csv_path) as states_csv:
        reader = csv.DictReader(states_csv)
        for r in reader:
            states[r['Abbreviation']] = r[value_field].replace(' ', '_')

    return states


def download_acs_data():
    """"""

    # get raw census data in text delimited form, the data has been
    # grouped into what the Census Bureau calls 'sequences'
    acs_url = 'http://www2.census.gov/programs-surveys/' \
              'acs/summary_file/{yr}'.format(yr=ops.acs_year)

    for geog in ACS_GEOGRAPHY:
        geog_dir = join(ops.data_dir, geog.lower())

        if not exists(geog_dir):
            os.makedirs(geog_dir)

        for st in ops.states:
            st_name = state_names[st]
            geog_url = '{base_url}/data/{span}_year_by_state/' \
                       '{state}_{geography}.zip'.format(
                            base_url=acs_url, span=ops.span,
                            state=st_name, geography=geog)

            geog_path = download_with_progress(geog_url, geog_dir)
            with ZipFile(geog_path, 'r') as z:
                print '\nunzipping...'
                z.extractall(dirname(geog_path))

    # the raw csv doesn't have field names for metadata, the templates
    # downloaded below provide that (but only the geoheader metadata
    # will be used by this process)
    schema_url = '{base_url}/data/{yr}_{span}yr_' \
                 'Summary_FileTemplates.zip'.format(
                      base_url=acs_url, yr=ops.acs_year, span=ops.span)

    schema_path = download_with_progress(schema_url, ops.data_dir)
    with ZipFile(schema_path, 'r') as z:
        print '\nunzipping...'
        z.extractall(dirname(schema_path))

    # download the lookup table that contains information as to how to
    # extract the ACS tables from the sequences
    lookup_url = '{base_url}/documentation/user_tools/' \
                 '{lookup}'.format(base_url=acs_url, lookup=ops.lookup_file)
    download_with_progress(lookup_url, ops.data_dir)


def download_with_progress(url, dir):
    """"""

    # function adapted from: http://stackoverflow.com/questions/22676

    file_name = url.split('/')[-1]
    file_path = join(dir, file_name)
    u = urllib2.urlopen(url)
    f = open(file_path, 'wb')
    meta = u.info()
    file_size = int(meta.getheaders("Content-Length")[0])
    print "Downloading: %s Bytes: %s" % (file_name, file_size)

    file_size_dl = 0
    block_sz = 8192
    while True:
        buffer_ = u.read(block_sz)
        if not buffer_:
            break

        file_size_dl += len(buffer_)
        f.write(buffer_)

        status = '{0:10d}  [{2:3.2f}%]'.format(
            file_size_dl, file_size, file_size_dl * 100. / file_size)
        status += chr(8) * (len(status) + 1)
        print status,

    f.close()

    return file_path


def drop_create_acs_schema():
    """"""

    engine = ops.engine
    schema = ops.metadata.schema
    engine.execute("DROP SCHEMA IF EXISTS {} CASCADE;".format(schema))
    engine.execute("CREATE SCHEMA {};".format(schema))


def create_geoheader():
    """"""

    geo_xls = '{yr}_SFGeoFileTemplate.xls'.format(yr=ops.acs_year)
    geo_schema = join(ops.data_dir, geo_xls)
    book = xlrd.open_workbook(geo_schema)
    sheet = book.sheet_by_index(0)

    columns = []
    blank_counter = 1
    for cx in xrange(sheet.ncols):
        # there are multiple fields called 'blank' that are reserved
        # for future use, but columns in the same table cannot have
        # the same name
        col_name = sheet.cell_value(0, cx).lower()
        if col_name == 'blank':
            col_name += str(blank_counter)
            blank_counter += 1

        cur_col = Column(
            name=col_name,
            type_=Text,
            doc=sheet.cell_value(1, cx).encode('utf8')
        )
        if cur_col.name.upper() in ACS_PRIMARY_KEY:
            cur_col.primary_key = True
        else:
            cur_col.primary = False

        columns.append(cur_col)

    print '\ncreating geoheader...'

    tbl_name = 'geoheader'
    tbl_comment = 'Intermediary table used to join ACS and TIGER data'
    table = Table(
        tbl_name,
        ops.metadata,
        *columns,
        info=tbl_comment)
    table.create()
    add_database_comments(table)

    geog_dir = join(ops.data_dir, ACS_GEOGRAPHY[0].lower())
    for st in ops.states:
        geo_csv = 'g{yr}{span}{state}.csv'.format(
            yr=ops.acs_year, span=ops.span, state=st.lower()
        )
        with open(join(geog_dir, geo_csv)) as geo_data:
            reader = csv.reader(geo_data)
            for row in reader:
                # null values come in from the csv as empty strings
                # this converts them such that they will be NULL in
                # the database
                null_row = [None if v == '' else v for v in row]
                table.insert(null_row).execute()


def create_acs_tables():
    """"""

    acs_tables = dict()
    lookup_path = join(ops.data_dir, ops.lookup_file)

    # this csv is encoded as cp1252 (aka windows-1252) this some of the
    # strings contain characters that need to be decoded as such
    with open(lookup_path) as lookup:
        reader = csv.DictReader(lookup)
        for row in reader:
            if row['Start Position'].isdigit():
                meta_table = {
                    'name': row['Table ID'].lower(),
                    'sequence': row['Sequence Number'],
                    'start_ix': int(row['Start Position']) - 1,
                    'cells': int(''.join(
                        [i for i in row['Total Cells in Table']
                         if i.isdigit()])),
                    'comment': row['Table Title'],
                    'columns': [
                        Column(
                            name='stusab',
                            type_=Text,
                            doc='State Postal Abbreviation',
                            primary_key=True
                        ),
                        Column(
                            name='logrecno',
                            type_=Text,
                            doc='Logical Record Number',
                            primary_key=True
                        )
                    ]
                }
                acs_tables[row['Table ID']] = meta_table

            # the universe of the table subject matter is stored in a
            # separate row, add it to the table comment
            elif not row['Line Number'].strip() \
                    and not row['Start Position'].strip():
                cur_tbl = acs_tables[row['Table ID']]
                cur_tbl['comment'] += ', {}'.format(row['Table Title'])

            # note that there are some rows with a line number of '0.5'
            # I'm not totally clear on what purpose they serve, but they
            # are not row in the tables and are being excluded here.
            elif row['Line Number'].isdigit():
                cur_tbl = acs_tables[row['Table ID']]
                cur_col = Column(
                    name='_' + row['Line Number'],
                    type_=Numeric,
                    doc=row['Table Title']
                )
                cur_tbl['columns'].append(cur_col)

    # a few values need to be scrubbed in the source data, this
    # dictionary defines those mappings
    scrub_map = {k.lower(): k for k in state_names.keys()}
    scrub_map.update({
        '': None,
        '.': 0
    })

    stusab_ix, logrec_ix = 2, 5

    print 'creating acs tables, this will take awhile...'
    print 'currently building table:'

    for mt in acs_tables.values():

        # there are two variants for each table one contains the actual
        # data and other contains the corresponding margin of error for
        # each cell
        table_variant = {
            'standard': {
                'file_char': 'e',
                'name_ext': '',
                'meta_table': mt
            },
            'margin of error': {
                'file_char': 'm',
                'name_ext': '_moe',
                'meta_table': deepcopy(mt)}}

        for tv in table_variant.values():
            mtv = tv['meta_table']
            mtv['name'] += tv['name_ext']

            print '\x1b[2k{}\r',
            print '{}\r'.format(mtv['name']),

            table = Table(
                mtv['name'],
                ops.metadata,
                *mtv['columns'],
                info=mtv['comment'])
            table.create()
            add_database_comments(table, 'cp1252')

            # create a list of the indices that for the columns that will
            # be extracted from the defined sequence for the current table
            columns = [stusab_ix, logrec_ix]
            columns.extend(
                xrange(mtv['start_ix'], mtv['start_ix'] + mtv['cells'])
            )

            memory_tbl = list()
            for st in ops.states:
                seq_name = '{type}{yr}{span}{state}{seq}000.txt'.format(
                    type=tv['file_char'], yr=ops.acs_year, span=ops.span,
                    state=st.lower(), seq=mtv['sequence'])

                for geog in ACS_GEOGRAPHY:
                    seq_path = join(ops.data_dir, geog, seq_name)
                    with open(seq_path) as seq:
                        reader = csv.reader(seq)
                        for row in reader:
                            tbl_ix = 0
                            tbl_row = dict()
                            for ix in columns:
                                try:
                                    row[ix] = scrub_map[row[ix]]
                                except KeyError:
                                    pass

                                field_name = mtv['columns'][tbl_ix].name
                                tbl_row[field_name] = row[ix]
                                tbl_ix += 1

                            memory_tbl.append(tbl_row)

            # this type bulk of insert uses sqlalchemy core and
            # is faster than alternative methods see details here:
            # http://docs.sqlalchemy.org/en/rel_0_8/faq.html#
            # i-m-inserting-400-000-rows-with-the-orm-and-it-s-really-slow
            ops.engine.execute(table.insert(), memory_tbl)


def add_database_comments(table, encoding=None):
    """Add comments to the supplied table and each of its columns, the
    meaning of each table and column in the ACS can be difficult to
    ascertain and this should help to clarify"""

    schema = ops.metadata.schema
    with ops.engine.begin() as connection:
        # using postgres dollar quotes on comment as some of the
        # comments contain single quotes
        tbl_comment_sql = "COMMENT ON TABLE " \
                          "{schema}.{table} IS $${comment}$$;".format(
                                schema=schema, table=table.name,
                                comment=table.info)
        connection.execute(tbl_comment_sql)

        col_template = r"COMMENT ON COLUMN " \
                       "{schema}.{table}.{column} IS $${comment}$$;"
        for c in table.columns:
            # sqlalchemy throws an error when there is a '%' sign in a
            # query thus they must be escaped with a second '%%' sign
            col_comment_sql = col_template.format(
                schema=schema, table=table.name,
                column=c.name, comment=c.doc.replace('%', '%%'))

            # the files from which comments are derived have non-ascii
            # encoded files and thus need to be appropriately decoded
            # as such
            if encoding:
                col_comment_sql = col_comment_sql.decode(encoding)

            connection.execute(col_comment_sql)


def process_options(arg_list=None):
    """"""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-s', '--states',
        nargs='+',
        required=True,
        choices=sorted(state_names.keys()),
        help='states for which data is to be include in acs database, '
             'indicate states with two letter postal codes'
    )
    parser.add_argument(
        '-y', '--year',
        required=True,
        dest='acs_year',
        help='most recent year of desired ACS data product'
    )
    parser.add_argument(
        '-l', '--span', '--length',
        default=5,
        choices=(1, 3, 5),
        help='number of years that ACS data product covers'
    )
    parser.add_argument(
        '-dd', '--data_directory',
        default=join(os.getcwd(), 'data', 'ACS'),
        dest='data_dir',
        help='file path at which downloaded ACS data is to be saved'
    )
    parser.add_argument(
        '-H', '--host',
        default='localhost',
        help='url of postgres host server'
    )
    parser.add_argument(
        '-u', '--user',
        default='postgres',
        help='postgres user name'
    )
    parser.add_argument(
        '-d', '--dbname',
        default='census',
        help='name of target database'
    )
    parser.add_argument(
        '-p', '--password',
        required=True,
        help='postgres password for supplied user'
    )

    options = parser.parse_args(arg_list)
    return options


def main():
    """"""

    global state_names
    state_names = get_states_mapping('name')

    global ops
    args = sys.argv[1:]
    ops = process_options(args)

    pg_conn_str = 'postgres://{user}:{pw}@{host}/{db}'.format(
        user=ops.user, pw=ops.password, host=ops.host, db=ops.dbname)

    ops.engine = create_engine(pg_conn_str)
    ops.lookup_file = 'ACS_{span}yr_Seq_Table_Number_' \
                      'Lookup.txt'.format(span=ops.span)
    ops.metadata = MetaData(
        bind=ops.engine,
        schema='acs{yr}_{span}yr'.format(yr=ops.acs_year,
                                         span=ops.span))

    # download_acs_data()
    drop_create_acs_schema()
    # create_geoheader()
    create_acs_tables()


if __name__ == '__main__':
    main()
