import os,sys
import json, ast
path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../cgsdatatools'))
if not path in sys.path:
    sys.path.insert(1, path)
del path
import string
import collections
import shutil
import vcf
import os
import avro
from avro.io import DatumReader, DatumWriter
from avro.datafile import DataFileReader, DataFileWriter

from beeswax.design import hql_query
from beeswax.server import dbms
from beeswax.server.dbms import get_query_server_config
from subprocess import *
import json
import time

class formatConverters(object):
    """
    Format converters

    Possible formats:
        * input: vcf, vcf.gz (gzipped), json, jsonflat
        * output: json, jsonflat, avro, parquet
        * additional file: avsc (avro schema)  
    """
    def __init__(self,
                 input_file,
                 output_file,
                 input_type = "",
                 output_type = "",
                 converting_method = "default"):
        
        self.input_file = input_file
        self.output_file = output_file
        if input_type == "":
            sp = input_file.split('.')
            self.input_type = sp[len(sp)-1]
            if self.input_type == 'gz':
                self.input_type = sp[len(sp)-2] + sp[len(sp)-1]
        else:
            self.input_type = input_type
            
        if output_type == "":
            sp = output_file.split('.')
            self.output_type = sp[len(sp)-1]
        else:
            self.output_type = output_type
    
        self.converting_method = converting_method

    def show(self):
        print("""
        Input file: %s
        Output file: %s
        Converting method""" % (self.input_type, self.output_type, self.converting_method))

    def convertVcfToFlatJson(self, request, organization="ulb", analysis="0", initial_file="no-file.vcf"):
        """
            Convert a vcf file to a flat json file
            Check the doc: https://pyvcf.readthedocs.org/en/latest/API.html#vcf-model-call
            Be careful: we do not respect exactly the google genomics structure in our database (but the api respects it)
        """
        if self.input_type not in ['vcf','vcf.gz'] or self.output_type != 'jsonflat':
            msg = "Error: vcf files (possibly gzipped) must be given as input files, and a jsonflat file should be given as output file."
            status = "failed"
            raise ValueError(msg)

        mapping = self.getMappingPyvcfToJson()

        f = open(self.input_file, 'r')
        o = open(self.output_file, 'w')

        list_of_rowkeys = []
        list_of_columns = []
        list_of_samples = []
        vcf_reader = vcf.Reader(f)
        for record in vcf_reader:
            linedic = {}

            for s in record.samples:
                # We initialize the flat json (the specific name for the call column for each sample will be computed below)
                linedic['variants.calls[]'] = {'info{}':{},'genotypeLikelihood[]':[],'genotype[]':[]}
                linedic['variants.alternateBases[]'] = []
                linedic['variants.filters[]'] = []

                # The most important thing to start with: check if the current sample has an alternate or not!
                # This is almost mandatory, otherwise you create many sample with no alternate (for 1000genomes
                # you create an enormous amount of data if you don't care about that -_-)
                if not s.is_variant:
                    # We are not fetching a variant, so we can skip the information...
                    continue

                # We get the data common for all the samples
                if hasattr(s.data,'DP'):
                    linedic['variants.calls[]']['info{}']['read_depth'] = s.data.DP
                else:
                    linedic['variants.calls[]']['info{}']['read_depth'] = "NA"


                if len(uniqueInList(linedic['variants.calls[]']['genotype[]'])) > 1:
                    call_het = "Heterozygous"
                else:
                    call_het = "Homozygous"
                if isinstance(record.ALT, list):
                    linedic['variants.alternateBases[]'] = '|'.join([str(a) for a in record.ALT])
                    linedic['variants.calls[]']['genotype[]'] = '|'.join([str(a) for a in record.ALT])
                else:
                    linedic['variants.calls[]']['genotype[]'] = ["Error..."]

                if isinstance(record.FILTER, list):
                    linedic['variants.filters[]'] = record.FILTER

                # Now we map each additional data depending on the configuration
                for pyvcf_parameter in mapping:

                    if mapping[pyvcf_parameter] == 'variants.calls[]' or mapping[pyvcf_parameter] == 'variants.calls[].info{}':
                        continue

                    # We detect how to take the information from PyVCF, then we take it
                    if pyvcf_parameter == 'Record.ALT':
                        value = '|'.join([str(a) for a in record.ALT])
                    elif pyvcf_parameter.startswith('Record.INFO'):
                        field = pyvcf_parameter.split('.')
                        try:
                            value = record.INFO[field.pop()]
                        except:
                            value = ""
                    elif pyvcf_parameter.startswith('Record'):
                        field = pyvcf_parameter.split('.')
                        try:
                            value = str(getattr(record, field.pop()))
                        except:
                            value = ""

                        if value is None:
                            value = ""
                    elif pyvcf_parameter.startswith('Call'):
                        field = pyvcf_parameter.split('.')
                        try:
                            value = str(getattr(s, field.pop()))
                        except:
                            value = ""

                        if value is None:
                            value = ""
                    else:
                        value = ""
                        print("Parameter '"+pyvcf_parameter+"' not supported.")

                    # Now we decide how to store the information in json
                    if mapping[pyvcf_parameter] == 'variants.alternateBases[]':
                        pass
                    elif mapping[pyvcf_parameter] == 'variants.calls[].genotype[]':
                        linedic['variants.calls[]']['genotype[]'] = linedic['variants.alternateBases[]']
                    elif mapping[pyvcf_parameter].startswith('variants.calls[].info{}'):
                        tmp = mapping[pyvcf_parameter].split('variants.calls[].info{}')
                        linedic['variants.calls[]']['info{}'][tmp[1]] = value
                    elif mapping[pyvcf_parameter].startswith('variants.calls[].'):
                        tmp = mapping[pyvcf_parameter].split('variants.calls[].')
                        if tmp[1] != 'info{}':
                            linedic['variants.calls[]'][tmp[1]] = value
                    else:
                        linedic[mapping[pyvcf_parameter]] = value

                # Some information we need to compute ourselves
                linedic['variants.variantSetId'] = analysis+'|'+initial_file
                linedic['variants.calls[]']['callSetId'] = s.sample
                linedic['variants.calls[]']['callSetName'] = s.sample

                # We have to add the sample id for the current sample
                linedic['variants.calls[]']['info{}']['sampleId'] = s.sample

                # TODO: Here you should annotate the variant with external databases inside variants.calls[].info{}!

                if linedic['variants.calls[]']['info{}']['sampleId'] not in list_of_samples:
                    list_of_samples.append(linedic['variants.calls[]']['info{}']['sampleId'])

                # Before writing the data to the json flat, we need to format them according to the avsc file
                # and the current sample id
                rowkey = organization + '|' + analysis + '|' + linedic['variants.referenceName'] + '|' + linedic['variants.start'] + '|' + linedic['variants.referenceBases'] + '|' + linedic['variants.alternateBases[]'][0]
                linedic['variants.id'] = rowkey
                linedic['variants.calls[].'+rowkey] = json.dumps(linedic['variants.calls[]'])
                del linedic['variants.calls[]']
                if rowkey not in list_of_rowkeys:
                    list_of_rowkeys.append(rowkey)

                # We do not do a json.dumps for other columns than variants.calls[], except for variants.info{}
                for jsonkey in linedic:
                    if type(linedic[jsonkey]) is list:
                        if len(linedic[jsonkey]) > 0 :
                            linedic[jsonkey] = '|'.join(linedic[jsonkey])
                    elif type(linedic[jsonkey]) is dict:
                        linedic[jsonkey] = json.dumps(linedic[jsonkey])

                    if jsonkey not in list_of_columns:
                        list_of_columns.append(jsonkey)

                o.write(json.dumps(linedic, ensure_ascii=False) + "\n")
        o.close()
        f.close()

        status = "succeeded"
        return status, list_of_columns, list_of_samples, list_of_rowkeys

    def convertHbaseToAvro(self,avscFile = "", add_default=True, modify=True):
        """
            Convert an hbase json file to an avro file using AVSC for making the conversion
            http://avro.apache.org/docs/1.7.6/gettingstartedpython.html
        """

        with open(avscFile,'r') as content_file:
            avro_schema = json.loads(content_file.read())
        columns_lookup = {}
        for field in avro_schema['fields']:
            if 'default' in field:
                columns_lookup[field['name']] = field['default']
            else:
                columns_lookup[field['name']] = 'NONE'

        status = "failed"

        if avscFile == "":
            msg = "This feature is not yet implemented. Please provide an AVRO schema file (.avsc)."
            raise ValueError(msg)
        else:
            schema = avro.schema.parse(open(avscFile).read())
            writer = DataFileWriter(open(self.output_file, "w"), DatumWriter(), schema)
            h = open(self.input_file)
            i = 0
            st = time.time()
            lines = []
            while 1: ## reading line per line in the flat json file and write them in the AVRO format
                line = h.readline()
                if not line:
                    break
                ls = line.strip()
                data = json.loads(ls)

                if modify is True:
                    # We need to replace the ';' in the file to an '_'
                    modified_data = {}
                    for key in data:
                        modified_data[key.replace(':','_')] = data[key]
                    data = modified_data

                if add_default is True and False:
                    # We need to add ourselves the default values for each call even if the avsc file does contain a 'default' parameter :/.
                    for field_name in columns_lookup:
                        if field_name not in data:
                            data[field_name] = columns_lookup[field_name]

                i += 1
                if i % 100 == 0:
                    tmpf = open('superhello.txt','a')
                    tmpf.write('Converter for line '+str(i)+': '+str(time.time()-st)+' > len dict: '+str(len(data))+'\n')
                    tmpf.close()
                # We finally write the avro file
                #writer.append(ast.literal_eval(ls))
                writer.append(data)
            h.close()
            writer.close()
            status = "succeeded"
        return(status)

        ## cmd = "java -jar ../avro-tools-1.7.7.jar fromjson --schema-file" + avscFile + " " + self.input_file > self.output_file

    def convertFlatJsonToHbase(self):
        """
            Convert a flat json file to an hbase json file. It's mostly a key mapping so nothing big
        """

        # 1st: we take the json to hbase information
        mapping = self.getMapping()

        json_to_hbase = {}
        types = {}
        for key in mapping:
            json_to_hbase[mapping[key]['json']] = mapping[key]['hbase'].replace('.',':')
            types[mapping[key]['json']] = mapping[key]['type']

        # 2nd: we create a temporary file in which we will save each future line for HBase
        f = open(self.input_file, 'r')
        o = open(self.output_file, 'w')

        for json_line in f:
            variant = json.loads(json_line)

            output_line = {}
            output_line['pk'] = variant['variants.id']
            output_line['rowkey'] = variant['variants.id']
            for attribute in variant:

                if attribute.startswith('variants.calls[]'):
                    # We generate the table name based on the 'sampleId' and the 'id' field (containing the information on the current analysis)
                    call_info = json.loads(variant[attribute])
                    hbase_key = 'I:CALL_'+call_info['info{}']['sampleId']
                else:
                    hbase_key = json_to_hbase[attribute]

                if attribute in types and types[attribute] == 'int':
                    try:
                        output_line[hbase_key] = int(variant[attribute])
                    except:
                        output_line[hbase_key] = 0
                else:
                    output_line[hbase_key] = str(variant[attribute])

            # We generate the line
            o.write(json.dumps(output_line)+'\n')
        f.close()
        o.close()

        status = "succeeded"
        return status

    def convertHbaseToText(self, hbase_data):
        # Convert data from an HBase json object (generated from a file with extension '.hbase') to a '.tsv' file
        # we return a string to put inside a tsv in fact
        pass


    def convertJsonToText(self, request):
        # Obsolete
        # The json received should be created previously by 'convertPyvcfToJson' as we will want a json object/line

        # 1st: we take the json to text information
        mapping = self.getMappingJsonToText()
        max_items = 0
        for key in mapping:
            if mapping[key] > max_items:
                max_items = mapping[key]

        # 2nd: we create the tsv file
        f = open(self.input_file, 'r')
        o = open(self.output_file, 'w')
        specific_columns = []

        for json_line in f:
            variant = json.loads(json_line)

            # We take the different alternates
            # TODO: for some reasons the json.loads() doesn't like the value it received...
            try:
                alternates = json.loads(variant['variants.alternateBases[]'])
            except:
                alternates = [variant['variants.alternateBases[]'].replace('[','').replace(']','')]

            for alternate in alternates:

                # We associate a json value to a position in the output
                output_line = ["" for i in range(max_items+1)]
                for json_key in mapping:
                    if json_key in variant:
                        output_line[mapping[json_key]] = str(variant[json_key])

                # We generate the rowkey
                output_line[0] = variant['variants.referenceName'] + '-' + variant['variants.start'] + '-' + variant['variants.referenceBases'] + '-' + alternate

                # We generate the line
                o.write('='.join(output_line).replace('"','')+'\n')

        f.close()
        o.close()

        status = "succeeded"
        return(status)

    def convertJsonToHBase(self, request, analysis, organization):
        # The json received should be created previously by 'convertPyvcfToJson' as we will want a json object/line
        # We will create a json as output too, but it will be adapted to the one used in HBase

        # 1st: we take the json to text information
        mapping = self.getMapping()

        json_to_hbase = {}
        for key in mapping:
            json_to_hbase[mapping[key]['json']] = mapping[key]['hbase'].replace('.',':')

        # 2nd: we create a temporary file in which we will save each future line for HBase
        f = open(self.input_file, 'r')
        o = open(self.output_file, 'w')

        for json_line in f:
            variant = json.loads(json_line)

            output_line = {}
            rowkey = organization + '-' + analysis + '-' + variant['variants.referenceName'] + '-' + variant['variants.start'] + '-' + variant['variants.referenceBases'] + '-' + variant['variants.alternateBases[]'][0]
            output_line['rowkey'] = rowkey
            variant['variants.id'] = rowkey
            for attribute in variant:

                if attribute == 'variants.calls[]':
                    # Specific case for the variants.calls[] (in fact, it will be variants.calls[0], variants.calls[1], ...

                    for call in variant[attribute]:
                        # We take the sample id associated to this call
                        if not 'info{}' in call:
                            continue
                        sampleId = call['info{}']['sampleId']
                        variantId = variant['variants.id']

                        # We generate the table name based on the 'sampleId' and the 'id' field (containing the information on the current analysis)
                        table_name_for_call = hbaseTableName(variantId, sampleId)

                        # We got through the different fields for this object
                        subline = {}
                        for subattribute in call:
                            if subattribute == 'info{}':
                                for infokey in call[subattribute]:
                                    if subattribute in subline: # each dict info is separated through '|'
                                        subline[subattribute] += '|'
                                    else:
                                        subline[subattribute] = ''

                                    if type(call[subattribute][infokey]) is list:
                                        # The first element of multiple values separated by ';' is the info key.
                                        subline[subattribute] += infokey+';'+';'.join(str(value) for value in call[subattribute][infokey])
                                    else:
                                        subline[subattribute] += infokey+';'+str(call[subattribute][infokey])
                            else:
                                if type(call[subattribute]) is list:
                                    subline[subattribute] = ';'.join(str(value) for value in call[subattribute])
                                else:
                                    subline[subattribute] = str(call[subattribute])

                            if subline[subattribute] == "None":
                                subline[subattribute] = ""

                        # We merge the information for the given call.
                        output_line[table_name_for_call] = '|'.join(key+'|'+value for key, value in subline.iteritems())

                elif attribute == 'info{}':
                    for infokey in variant[attribute]:
                        if attribute in output_line: # each dict info is separated through '|'
                            output_line[attribute] += '|'

                        if type(variant[attribute][infokey]) is list:
                            # The first element of multiple values separated by ';' is the info key.
                            output_line[attribute] += infokey+';'+';'.join(str(value) for value in variant[attribute][infokey])
                        else:
                            output_line[attribute] += infokey+';'+str(variant[attribute][infokey])

                elif type(attribute) is list:
                    output_line[json_to_hbase[attribute]] = ';'.join(str(value) for value in variant[attribute])
                else:
                    output_line[json_to_hbase[attribute]] = str(variant[attribute])

            # We generate the line
            o.write(json.dumps(output_line)+'\n')
        f.close()
        o.close()

        status = "succeeded"
        return(status)

    def convertJSON2FLATJSON(self):
        """ Convert a JSON file (for the format, see the documentation) to a flat JSON file or more accurately a series of JSON lines  
        """
        if self.input_type != 'json' or self.output_type != 'json':
            msg = "Error: json files must be given as input files."
            status = "failed"
            raise ValueError(msg)
        
        f = open(self.input_file)
        h = open(self.output_file,'w')
        line = f.readline()
        jsl = json.loads(line)
        try:
            for i in jsl.keys():
                flatJSON = flatten(jsl[i])
                flatJSONLiteral = ast.literal_eval(json.dumps(flatJSON))
                h.write(str(flatJSONLiteral).replace("'",'"').replace(".","_") + '\n')
            status = "succeeded"
        except:
            msg = "Error: the json does not follow the right syntax."
            status = "failed"
            raise ValueError(msg)
        return(status)
        f.close()
        h.close()

    def getMappingJsonToText(self):
        # Return the mapping 'json_parameter' > 'order_in_text_file'

        mapping = self.getMapping()

        new_mapping = {}
        for key in mapping:
            new_mapping[mapping[key]['json']] = mapping[key]['parquet']

        return new_mapping

    def getMappingPyvcfToText(self):
        # Return the mapping 'pyvcf_parameter' > 'order_in_text_file'

        mapping = self.getMapping()

        new_mapping = {}
        for key in mapping:
            new_mapping[key] = mapping[key]['parquet']

        return new_mapping

    def getMappingPyvcfToJson(self):
        # Return the mapping PyVCF to JSON
        mapping = self.getMapping()

        new_mapping = {}
        for key in mapping:
            new_mapping[key] = mapping[key]['json']

        return new_mapping

    def getMappingJsonToHBase(self):
        # Return the mapping Json to HBase
        mapping = self.getMapping()

        new_mapping = {}
        for key in mapping:
            new_mapping[mapping[key]['json']] = mapping[key]['hbase']

        return new_mapping

    def getMappingJsonToParquet(self):
        # Return the mapping Json to Parquet field (we don't want to have the order for parquet, just the column names)
        mapping = self.getMapping()

        new_mapping = {}
        for key in mapping:
            new_mapping[mapping[key]['json']] = str(mapping[key]['hbase'].replace('.','_')).lower()

        return new_mapping

    def getMapping(self):
        # Return the mapping between PyVCF, JSON, HBase and Parquet (parquet position only)
        # Sometimes there is nothing in PyVCF to give information for a specific file created by ourselves.
        # DO NOT change the 'json' fields...

        mapping = {
        'Record.CHROM':{'json':'variants.referenceName','hbase':'R.C','parquet':1,'type':'string'},
           'Record.POS':{'json':'variants.start','hbase':'R.P','parquet':2,'type':'int'},
           'Record.REF':{'json':'variants.referenceBases','hbase':'R.REF','parquet':3,'type':'string'},
           'Record.ALT':{'json':'variants.alternateBases[]','hbase':'R.ALT','parquet':4,'type':'list'},
           'Record.ID':{'json':'variants.info.dbsnp_id','hbase':'I.DBSNP137','parquet':5,'type':'string'},
           'Record.FILTER':{'json':'variants.filters[]','hbase':'R.FILTER','parquet':6,'type':'list'},
           'Record.QUAL':{'json':'variants.quality','hbase':'R.QUAL','parquet':7,'type':'float'},
           'Record.INFO.QD':{'json':'variants.info.confidence_by_depth','hbase':'I.QD','parquet':8,'type':'string'},
           'Record.INFO.HRun':{'json':'variants.info.largest_homopolymer','hbase':'I.HR','parquet':9,'type':'string'},
           'Record.INFO.SB':{'json':'variants.strand_bias','hbase':'I.SB','parquet':10,'type':'string'},
           'Record.INFO.DP':{'json':'variants.calls[].info.read_depth','hbase':'F.DPF','parquet':11,'type':'string'},
           'Record.INFO.MQ0':{'json':'variants.info.mapping_quality_zero_read','hbase':'I.MQ0','parquet':12,'type':'string'},
           'Record.INFO.DS':{'json':'variants.info.downsampled','hbase':'I.DS','parquet':13,'type':'string'},
           'Record.INFO.AN':{'json':'variants.info.allele_num','hbase':'I.AN','parquet':14,'type':'string'},
           'Record.INFO.AD':{'json':'variants.calls[].info.confidence_by_depth','hbase':'F.AD','parquet':15,'type':'string'},
           'Call.sample':{'json':'readGroupSets.readGroups.sampleID','hbase':'R.SI','parquet':16,'type':'string'},

            # The following terms should be correctly defined
           'manual1':{'json':'variants.variantSetId','hbase':'R.VSI','parquet':17,'type':'string'},
           'todefine2':{'json':'variants.id','hbase':'R.ID','parquet':18,'type':'string'}, # Ok
           'Call.sample2':{'json':'variants.names[]','hbase':'R.NAMES','parquet':19,'type':'list'},
           'todefine4':{'json':'variants.created','hbase':'R.CREATED','parquet':20,'type':'int'},
           'todefine5':{'json':'variants.end','hbase':'R.PEND','parquet':21,'type':'int'},
           'todefine6':{'json':'variants.info{}','hbase':'R.INFO','parquet':22,'type':'dict'},
           'todefine7':{'json':'variants.calls[]','hbase':'R.CALLS','parquet':23,'type':'list'},
           'manual2':{'json':'variants.calls[].callSetId','hbase':'R.CALLS_ID','parquet':24,'type':'string'},
           'manual3':{'json':'variants.calls[].callSetName','hbase':'R.CALLS_NAME','parquet':25,'type':'string'},
           'Call.gt_bases':{'json':'variants.calls[].genotype[]','hbase':'R.CALLS_GT','parquet':26,'type':'list'},
           'Call.phased':{'json':'variants.calls[].phaseset','hbase':'R.CALLS_PS','parquet':27,'type':'string'},
           'todefine12':{'json':'variants.calls[].genotypeLikelihood[]','hbase':'R.CALLS_LHOOD','parquet':28,'type':'list'},
           'todefine13':{'json':'variants.calls[].info{}','hbase':'R.CALLS_INFO','parquet':29,'type':'dict'},
        }

        return mapping

def hbaseTableName(variantId, sampleId):
    # Return the hbase table name for a given variantId (generated by us, already containing information about the analysis)
    # and a sampleId

    # TODO: to improve, for now it is way too long
    return 'I:CALL_'+sampleId

def getHbaseColumns():
    # Return a list of the different columns for HBase
    fc = formatConverters(input_file='stuff.vcf',output_file='stuff.json')
    mapping = fc.getMapping()

    result = []
    for pyvcf in mapping:
        result.append(mapping[pyvcf]['hbase'].replace('.',':'))

    return result


def dbmap(json_term, database="impala", order=False):
    # Return the mapping between a given json name and a specific field name (for Impala typically, but it should be
    # the same for HBase, but we need to give the column family too). Returns None if nothing found.
    fc = formatConverters(input_file='stuff.vcf',output_file='stuff.json')
    mapping = fc.getMapping()

    value = None
    for pyvcf in mapping:
        if mapping[pyvcf]['json'] == json_term:
            if order is False: # We want the field name
                if database == 'impala':
                    value = mapping[pyvcf]['hbase']
                else: #if hbase
                    value = mapping[pyvcf]['hbase'].replace('.',':')
            else: # We want the field number
                value = mapping[pyvcf]['parquet']

    return value

def dbmap_length():
    # Return the number of fields inside parquet/hbase
    fc = formatConverters(input_file='stuff.vcf',output_file='stuff.json')
    mapping = fc.getMapping()

    max_number = 0
    for pyvcf in mapping:
        if mapping[pyvcf]['parquet'] > max_number:
            max_number = mapping[pyvcf]['parquet']

    return max_number

def dbmapToJson(data, database="impala"):
    # Map the data from a database line to a json object
    # The 'data' is received from impala, and we get something like ['NA06986-4-101620184-TAAC-T', '4', '101620184', 'TAAC', '[T]', 'None', '[]', '19', '', '', '', '3279', '', '', '2', '', 'NA06986']
    # so we cannot rely on the column name, only on the order of the fields
    # TODO: manage multiple objects
    # TODO: manage HBase data

    mapped = {}
    fc = formatConverters(input_file='stuff.vcf',output_file='stuff.json')
    mapping = fc.getMapping()

    for pyvcf in mapping:

        json_field = mapping[pyvcf]['json']
        order = mapping[pyvcf]['parquet']
        type = mapping[pyvcf]['type']

        try:
            if type == 'int':
                mapped[json_field] = int(data[order])
            elif type == 'float':
                mapped[json_field] = float(data[order])
            elif type == 'dict':
                mapped[json_field] = json.loads(data[order])
            elif type == 'list':
                mapped[json_field] = data[order].split(';')
            else:
                mapped[json_field] = data[order]
        except:
            if type == 'int':
                value = 0
            elif type == 'float':
                value = 0.0
            elif type == 'dict':
                value = {}
            elif type == 'list':
                value = []
            else:
                value = ''
            mapped[json_field] = value

    return mapped

def hbaseToJson(raw_data):
    # Map the data received from multiple entries (result.columns) of hbase with multiple columns to a JSON object
    # This function need to merge similar variants (=same chromosome, reference, ... but different alternates)
    # into one object, to return data like google genomics
    # The list of data received should belong to one variant at the end, we will exclude data with a rowkey containing
    # different information than the first one (we only accept different alternates)
    mapped = {}
    fc = formatConverters(input_file='stuff.vcf',output_file='stuff.json')
    mapping = fc.getMapping()

    # We remove the variants we will not use
    first_rowkey = raw_data[0].row
    interesting_rowkey = first_rowkey.split('|')
    interesting_rowkey.pop()
    interesting_rowkey = '|'.join(interesting_rowkey)+'|'
    good_variants = []
    for hbase_variant in raw_data:
        if hbase_variant.row.startswith(interesting_rowkey):
            good_variants.append(hbase_variant)

    # We use a 'specific_variant' where we will take the data
    specific_variant = raw_data[0].columns

    # Basic data to map
    for pyvcf in mapping:

        json_field = mapping[pyvcf]['json']
        hbaseColumn = mapping[pyvcf]['hbase'].replace('.',':')
        type = mapping[pyvcf]['type']

        if json_field == 'variants.alternateBases[]':
            alts = []
            for good_variant in good_variants:
                alternatives = good_variant.columns[hbaseColumn].value.split('|')
                for alternative in alternatives:
                    if alternative not in alts:
                        alts.append(alternative)

            mapped[json_field] = alts
        else:
            try:
                if type == 'int':
                    mapped[json_field] = int(specific_variant[hbaseColumn].value)
                elif type == 'float':
                    mapped[json_field] = float(specific_variant[hbaseColumn].value)
                elif type == 'dict':
                    mapped[json_field] = json.loads(specific_variant[hbaseColumn].value)
                elif type == 'list':
                    mapped[json_field] = specific_variant[hbaseColumn].value.split(';')
                    if len(mapped[json_field]) == 1:
                        mapped[json_field] = specific_variant[hbaseColumn].value.split('|')
                else:
                    mapped[json_field] = specific_variant[hbaseColumn].value
            except:
                if type == 'int':
                    value = 0
                elif type == 'float':
                    value = 0.0
                elif type == 'dict':
                    value = {}
                elif type == 'list':
                    value = []
                else:
                    value = ''
                mapped[json_field] = value

    # Now we need to take care of calls (we cannot simply take information from specific_variant, we need to take
    # the data from all good_variants too)
    mapped['variants.calls[]'] = []
    for current_variant in good_variants:
        for hbase_field in current_variant.columns:
            if not hbase_field.startswith('I:CALL_'):
                continue
            try:
                call = json.loads(current_variant.columns[hbase_field].value)

                # We need to set the genotype[] value for the call, based on the different alts we generated above
                genotype_call = call['genotype[]']
                if genotype_call in alts:
                    genotype_id = 0
                    for alt in alts:
                        genotype_id += 1
                        if alt == genotype_call:
                            call['genotype[]'] = [genotype_id]
                            break
                else:
                    call['genotype[]'] = 'ERROR ('+genotype_call+')'

                mapped['variants.calls[]'].append(call)
            except:
                pass
    return mapped


def parquetToJson(raw_data):
    # Map the data received from multiples entries of parquet with multiple columns (we already have the name of columns in the keys) to a JSON object
    # This function need to merge similar variants (=same chromosome, reference, ... but different alternates)
    # into one object, to return data like google genomics
    # The list of data received should belong to one variant at the end, we will exclude data with a rowkey containing
    # different information than the first one (we only accept different alternates)

    mapped = {}
    fc = formatConverters(input_file='stuff.vcf',output_file='stuff.json')
    mapping = fc.getMapping()

    # We remove the variants we will not use
    first_rowkey = raw_data[0]['pk']
    interesting_rowkey = first_rowkey.split('|')
    interesting_rowkey.pop()
    interesting_rowkey = '|'.join(interesting_rowkey)+'|'
    good_variants = []
    for impala_variant in raw_data:
        if impala_variant['pk'].startswith(interesting_rowkey):
            good_variants.append(impala_variant)

    # Basic data to map
    specific_variant = good_variants[0]
    for pyvcf in mapping:

        json_field = mapping[pyvcf]['json']
        parquetColumn = str(mapping[pyvcf]['hbase'].replace('.','_')).lower()
        type = mapping[pyvcf]['type']

        if json_field == 'variants.alternateBases[]':
            alts = []
            for good_variant in good_variants:
                alternatives = good_variant[parquetColumn].split('|')
                for alternative in alternatives:
                    if alternative not in alts:
                        alts.append(alternative)

            mapped[json_field] = alts
        else:
            try:
                if type == 'int':
                    mapped[json_field] = int(specific_variant[parquetColumn])
                elif type == 'float':
                    mapped[json_field] = float(specific_variant[parquetColumn])
                elif type == 'dict':
                    mapped[json_field] = json.loads(specific_variant[parquetColumn])
                elif type == 'list':
                    mapped[json_field] = specific_variant[parquetColumn].split(';')
                    if len(mapped[json_field]) == 1:
                        mapped[json_field] = specific_variant[parquetColumn].split('|')
                else:
                    mapped[json_field] = specific_variant[parquetColumn]
            except:
                if type == 'int':
                    value = 0
                elif type == 'float':
                    value = 0.0
                elif type == 'dict':
                    value = {}
                elif type == 'list':
                    value = []
                else:
                    value = ''
                mapped[json_field] = value

    # Now we need to take care of calls
    mapped['variants.calls[]'] = []
    for current_variant in good_variants:
        for parquet_field in current_variant:
            if not parquet_field.startswith('i_call_'):
                continue
            if current_variant[parquet_field] != 'NA':
                try:
                    call = json.loads(current_variant[parquet_field])

                    # We need to set the genotype[] value for the call, based on the different alts we generated above
                    genotype_call = call['genotype[]']
                    if genotype_call in alts:
                        genotype_id = 0
                        for alt in alts:
                            genotype_id += 1
                            if alt == genotype_call:
                                call['genotype[]'] = [genotype_id]
                                break
                    elif genotype_call == mapped['variants.referenceBases']:
                        call['genotype[]'] = [0]
                    else:
                        call['genotype[]'] = 'ERROR ('+genotype_call+')'

                    mapped['variants.calls[]'].append(call)
                except:
                    pass

    return mapped

def jsonToSerializerData(json_data, fields, prefix):
    # Convert the json data from dbmapToJson to a data dict used by a DRF Serializer to initialize an object
    # The 'fields' come from the given Serializer. The 'prefix' comes also from the Serializer, it is based
    # on the hierarchy of the Serializer regarding the other Serializers (see google documentation)

    d = {}
    for field in fields:
        if prefix+'.'+field+'[]' in json_data:
            type = '[]'
        elif prefix+'.'+field+'{}' in json_data:
            type = '{}'
        else:
            type = ''

        try:
            d[field] = json_data[prefix+'.'+field+type]
        except:
            pass
    return d

def convertJSONdir2AVROfile(jsonDir, avroFile, avscFile):
    """ Convert all JSON files to one AVRO file
    """
    ## check if the input directory exists
    if not os.path.isdir(jsonDir):
        msg = "The directory %s does not exist" % jsonDir 
        raise ValueError(msg)
    
    ## check if the avsc file exists
    if not os.path.isfile(avscFile): 
        msg = "The file %s does not exist" % avscFile 
        raise ValueError(msg)
    
    ## convert JSON files to flat JSON files
    tmpJSONFLATDir = id_generator()
    os.makedirs(tmpJSONFLATDir)
    nbrJSONfiles = 0
    for f in os.listdir(jsonDir):
        if f.endswith(".json"):
            ft = f.replace(".json", "flat.json")
            converter = formatConverters(input_file = os.path.join(jsonDir,f) , output_file = os.path.join(tmpJSONFLATDir,ft))
            status = converter.convertJSON2FLATJSON()
            nbrJSONfiles += 1
            
    ## concat the flat JSON files into 1 flat JSON file 
    flatJSONFile = id_generator()
    o = open(flatJSONFile,"w")
    for f in os.listdir(tmpJSONFLATDir):
        h = open(os.path.join(tmpJSONFLATDir,f))
        while 1:
            line = h.readline()
            if not line:
                break
            o.write(line)
        h.close()
    o.close()
    
    ## reading the concatenated flat JSON file and write to AVRO file  
    converter = formatConverters(input_file = flatJSONFile, output_file = avroFile)
    status = converter.convertFLATJSON2AVRO(avscFile)
        
    ## cleaning up
    shutil.rmtree(tmpJSONFLATDir)
    os.remove(flatJSONFile)
    
    return(status)

def database_create_variants(request, temporary=False, specific_columns=None):
    # Create the variant table. If temporary is True, it means we need to create a temporary structure as Text to be imported
    # to another variants table (that won't be temporary). specific_columns eventually contain
    # the name of sample columns, like I.CALL_NA0787, we will verify if they are available, if not
    # we will alter the table
    if specific_columns is None:
        specific_columns = []

    result = {'value':True,'text':'Everything is alright'}

    # We install the tables for impala, based on the configuration
    fc = formatConverters(input_file='stuff.vcf',output_file='stuff.json',output_type='json')
    mapping = fc.getMapping()
    fields = fc.getMappingPyvcfToText()
    pyvcf_fields = fc.getMappingPyvcfToJson()
    hbase_fields = fc.getMappingJsonToHBase()
    inversed_fields = {}
    type_fields = {}
    max_value = 0
    for field in fields:
        if fields[field] > max_value:
            max_value = fields[field]
        future_field = hbase_fields[pyvcf_fields[field]].split('.')
        #inversed_fields[fields[field]] = future_field.pop()
        inversed_fields[fields[field]] = hbase_fields[pyvcf_fields[field]]

        try:
            type = mapping[field]['type']
        except:
            type = 'string'

        type_fields[fields[field]] = type

    # We add the specific fields for each variant
    for specific_column in specific_columns:
        max_value += 1
        inversed_fields[max_value] = specific_column
        type_fields[max_value] = 'string'

    variants_table = ["" for i in xrange(max_value+1)]
    for i in range(1, max_value + 1):
        if type_fields[i] == 'int':
            variants_table[i] = inversed_fields[i].replace('.','_')+" INT"
        else:
            variants_table[i] = inversed_fields[i].replace('.','_')+" STRING"

        if i < max_value:
            variants_table[i] += ","
    variants_table[0] = "pk STRING, "

    # Deleting the old table and creating the new one
    if temporary is True:
        query_server = get_query_server_config(name='hive')
        db = dbms.get(request.user, query_server=query_server)

        avro_schema = {"name": "variants","type": "record","fields": []}
        for field in variants_table:
            tmp = field.split(' ')
            name = tmp[0]
            type = tmp[1].split(',').pop(0).lower()

            if type == 'int':
                default_value = 0
            else:
                default_value = 'NA'

            avro_schema['fields'].append({'name':name,'type':type,'default':default_value})
        request.fs.create('/user/cgs/cgs_variants_'+request.user.username+'.avsc.json', overwrite=True, data=json.dumps(avro_schema))

        handle = db.execute_and_wait(hql_query("DROP TABLE IF EXISTS variants_tmp_"+request.user.username+";"), timeout_sec=30.0)
        query = hql_query("CREATE TABLE variants_tmp_"+request.user.username+"("+"".join(variants_table)+") stored as avro TBLPROPERTIES ('avro.schema.url'='hdfs://localhost:8020/user/cgs/cgs_variants_"+request.user.username+".avsc.json');")
        handle = db.execute_and_wait(query, timeout_sec=30.0)
    else:
        query_server = get_query_server_config(name='impala')
        db = dbms.get(request.user, query_server=query_server)

        handle = db.execute_and_wait(hql_query("DROP TABLE IF EXISTS variants;"), timeout_sec=30.0)
        query = hql_query("CREATE TABLE variants("+"".join(variants_table)+") stored as parquet;")
        handle = db.execute_and_wait(query, timeout_sec=30.0)

    # We install the variant table for HBase
    if temporary is False:
        try:
            hbaseApi = HbaseApi(user=request.user)
            currentCluster = hbaseApi.getClusters().pop()
            hbaseApi.createTable(cluster=currentCluster['name'],tableName='variants',columns=[{'properties':{'name':'I'}},{'properties':{'name':'R'}},{'properties':{'name':'F'}}])
        except:
            result['value'] = False
            result['text'] = 'A problem occured when connecting to HBase and creating a table. Check if HBase is activated. Note that this message will also appear if the \'variants\' table in HBase already exists. In that case you need to manually delete it.'

    return result, variants_table

def id_generator(size=6, chars=string.ascii_uppercase + string.digits):
    import random
    return ''.join(random.choice(chars) for x in range(size))

def is_number(s):
    try:
        float(s)
        return True
    except ValueError:
        pass

    try:
        import unicodedata
        unicodedata.numeric(s)
        return True
    except (TypeError, ValueError):
        pass

    return False

def flatten(d, parent_key='', sep='.'):
    items = []
    for k, v in d.items():
        new_key = parent_key + sep + k if parent_key else k
        if isinstance(v, collections.MutableMapping):
            items.extend(flatten(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)

def uniqueInList(seq):
    seen = set()
    seen_add = seen.add
    return [ x for x in seq if not (x in seen or seen_add(x))]
