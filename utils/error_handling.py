import copy

class ConfigError(Exception):
    # Errors in the config.py file
    pass

class ParseError(Exception):
    #Errors in parsing (generally documents not meeting parsing assumptions)
    pass

class InputError(Exception):
    #Errors in the input provided to the program.
    pass

def InputWarning(explanation):
    #Issues to warn about, but not stop execution.
    print("Input warning:")
    print(explanation + '\n\n')

def ParseWarning(explanation):
    #Issues to warn about, but not necessary to stop execution.
    print("Parsing Warning:")
    print(explanation + '\n\n')

def log_parsing_correction(file_path, correction_type, details, logfile_path=None):
    """
    Log instances where parsing corrections were applied for monitoring.
    
    Args:
        file_path (str): Path to the file where correction was applied
        correction_type (str): Type of correction (e.g., 'malformed_annex_structure')
        details (str): Description of the correction applied
        logfile_path (str, optional): Path to document issues logfile for structured logging
    """
    # Console output for immediate visibility
    print(f"[PARSING CORRECTION] {correction_type}")
    if file_path:
        print(f"  File: {file_path}")
    print(f"  Details: {details}")
    print()
    
    # Structured logging to document issues file if provided
    if logfile_path:
        import os
        import json
        from datetime import datetime
        
        log_entry = {
            'issue_type': 'parsing_correction',
            'correction_type': correction_type,
            'issue': details,
            'file_path': file_path,
            'timestamp': str(datetime.utcnow())
        }
        
        # Ensure logfile directory exists
        os.makedirs(os.path.dirname(logfile_path), exist_ok=True)
        
        # Append to logfile
        with open(logfile_path, 'a') as f:
            f.write(json.dumps(log_entry, indent=2))
            f.write('\n')
    
class ModelError(Exception):
    #Errors in interacting with the AI models.
    pass
    
def CheckVersion(parsed_content):
    if 'document_information' in parsed_content.keys():
        if not 'version' in parsed_content['document_information'].keys() or float(parsed_content['document_information']['version']) < 0.3:
            ParseError("Parsed document format in unsupported version.")
            exit(1)
            # parsed_content['document_information']['version'] = '0.2'
            # # Need to move parameters and organization objects into the document_information object.
            # if 'parameters' in parsed_content.keys():
            #     parsed_content['document_information']['parameters'] = copy.deepcopy(parsed_content['parameters'])
            #     del parsed_content['parameters']
            # if 'organization' in parsed_content.keys():
            #     parsed_content['document_information']['organization'] = copy.deepcopy(parsed_content['organization'])
            #     del parsed_content['organization']                
            # # Need to move organizational content to the document_information object.
            # if ('organization' in parsed_content['document_information'].keys() 
            #     and '1' in parsed_content['document_information']['organization'].keys()
            #     and 'names' in parsed_content['document_information']['organization']['1'].keys()
            #     and parsed_content['document_information']['organization']['1']['names'] in parsed_content.keys()):
            #         item_type_names = parsed_content['document_information']['organization']['1']['names']
            #         parsed_content['document_information']['organization']['content'] = {}
            #         parsed_content['document_information']['organization']['content'][item_type_names] = copy.deepcopy(parsed_content[item_type_names])
            #         del parsed_content[item_type_names]
            # # Move content to its own object.
            # if not 'content' in parsed_content.keys():
            #     parsed_content['content'] = {}
            # for item_type in parsed_content['document_information']['parameters'].keys():
            #     item_type_names = parsed_content['document_information']['parameters'][item_type]['names']
            #     if item_type_names in parsed_content.keys():
            #         parsed_content['content'][item_type_names] = copy.deepcopy(parsed_content[item_type_names])
            #         del parsed_content[item_type_names]
            # # Move definitions and indirect_definitions
            # if 'definitions' in parsed_content.keys():
            #     parsed_content['document_information']['definitions'] = copy.deepcopy(parsed_content['definitions'])
            #     del parsed_content['definitions']
            # if 'indirect_definitions' in parsed_content.keys():
            #     parsed_content['document_information']['indirect_definitions'] = copy.deepcopy(parsed_content['indirect_definitions'])
            #     del parsed_content['indirect_definitions']                    
    else:
        ParseError("No document_information found.")
        exit(1)