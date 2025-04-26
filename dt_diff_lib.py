from tree_sitter import Parser, Node
from tree_sitter_languages import get_language
import difflib
import re
import uuid

def get_changed_line_numbers( head_content, commit_content, count_on_head_commit):
    changed_lines = set()
    if head_content:

        diff = list(difflib.unified_diff(
        head_content.splitlines(), commit_content.splitlines(), n=0
        ))

        new_line_num = 0
        for line in diff:
            if line.startswith('@@'):
                match = re.match(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
                if match:
                    if not count_on_head_commit:
                        new_line_num = int(match.group(3)) - 1  # offset before first line in hunk
                    else:
                        new_line_num = int(match.group(1)) - 1  # offset before first line in hunk
            elif line.startswith('+') and not line.startswith('+++'):
                if not count_on_head_commit:
                    new_line_num += 1
                    changed_lines.add(new_line_num)
                else:
                    continue
            elif line.startswith('-') and not line.startswith('---'):
                if not count_on_head_commit:
                    continue  # don't increment line number
                else: 
                    new_line_num += 1
                    changed_lines.add(new_line_num)
            else:
                new_line_num += 1  # context line
    elif not head_content and not count_on_head_commit:
        counter = 1
        for line in commit_content.splitlines():
            changed_lines.add(counter)
            counter += 1
    return changed_lines

def tree_sitter_parser_init(file_language, content):
    parser = Parser()
    language = get_language(file_language)
    parser.set_language(language)
    tree = parser.parse(content)
    root_node = tree.root_node
    return root_node

class CommentNode:
    '''A class to enable merging leading comments into one object'''
    def __init__(self, nodes):
        self.nodes = nodes
        self.start_byte = nodes[0].start_byte
        self.end_byte = nodes[-1].end_byte
        self.start_point = (nodes[0].start_point[0]+1,nodes[0].start_point[1])
        self.end_point = (nodes[-1].end_point[0]+1,nodes[-1].end_point[1]+1)

    def __repr__(self):
        return f"<CommentNode comment>\n{self.content}"


def extract_data(use_diff, file_language, head_content, commit_content, handler_fn, build_tree_from_head_content = False):
    """
    Extract data from parsed source code using Tree-sitter, optionally using diff information.

    This function parses the provided source code with Tree-sitter and traverses the syntax tree
    to find function definitions. It can operate in diff-aware mode (only analyzing changed functions)
    or on the full file. For each qualifying function, a handler function is called with relevant data.

    Parameters
    ----------
    use_diff : bool
        If True, only analyze functions that include changed lines (based on diff between head and commit).
    file_language : str
        Language identifier used to initialize the Tree-sitter parser (e.g., "python", "javascript").
    head_content : str
        The content of the file at the head (used for diffing).
    commit_content : str
        The content of the file at the commit (parsed and analyzed).
    handler_fn : function
        A callback function that takes the form:
        `handler_fn(func_node, first_node, content, result_list)`
        and is called for each qualifying function.

    Returns
    -------
    list
        The accumulated results collected by the `handler_fn`.
    """    
    changed_lines = []
    if use_diff:
        changed_lines = get_changed_line_numbers(head_content, commit_content, build_tree_from_head_content)
        if not changed_lines:
            return []
    
    if not build_tree_from_head_content:
        root_node = tree_sitter_parser_init(file_language,commit_content.encode("utf-8"))
    else:
        root_node = tree_sitter_parser_init(file_language,head_content.encode("utf-8"))


    result = []
    nodeId = set()

    def traverse_functions(root_node, changed_lines, handler_fn):
        """
        Traverse the syntax tree and invoke handler_fn on qualifying function nodes.

        Parameters
        ----------
        root_node : tree_sitter.Node
            The root of the parsed syntax tree.
        changed_lines : list[int]
            List of changed line numbers (if diff is used).
        handler_fn : function
            A function which handles what kind of data to extract.
        """
        def visit(node):
            if node.id in nodeId:
                return
            else:
                nodeId.add(node.id)
                try:
                    if node.type == "function_definition":
                        #assessing lines is only relevant if you look at a diff
                        start_line = node.start_point[0] + 1
                        end_line = node.end_point[0] + 1
                        line_range = range(start_line, end_line + 1)
                        if any(line in changed_lines for line in line_range) or not use_diff:
                            block_node = next((child for child in node.children if child.type == "block"), None)
                            if not block_node or not block_node.children:
                                raise Exception("No block or block has no children in function")

                            first_node_in_block = block_node.children[0]
                                
                            handler_fn(func_node=node, first_node=first_node_in_block, content=commit_content, nodeIdSet=nodeId, result_list=result, mod_lines=set(changed_lines).intersection(line_range))
                            
                except Exception as e:
                    print(f"error: {e}")
                for child in node.children:
                    visit(child)

        visit(root_node)
    traverse_functions(root_node, changed_lines, handler_fn)
    return result 

def identify_comment_node(node, nodeIdSet):
    """
    Identify and return a CommentNode if the given node represents a comment or a group of leading comment nodes.

    This function inspects the given node and determines whether it is a comment-related node.
    - If the node is a line comment, it will attempt to collect consecutive leading comments
      (at the top of its parent block) and merge them into a single CommentNode.
    - If the node is a block comment or starts with a string (e.g., Python-style docstrings), it is wrapped directly.

    Parameters
    ----------
    node : tree_sitter.Node
        The Tree-sitter node to inspect.
    nodeIdSet : set
        A set used to track the IDs of processed comment nodes (to avoid reprocessing).

    Returns
    -------
    CommentNode or None
        A CommentNode object if the input node is a recognized comment type,
        otherwise None.
    """
    if node.type == "comment":
        # merge leading comments
        block_node = node.parent
        comment_nodes = []
        for child in block_node.children:
            if child.type == "comment":
                comment_nodes.append(child)
                nodeIdSet.add(child.id)
            else:
                break  # Stop when the first non-comment node is reached

        if comment_nodes:
            return CommentNode(comment_nodes)
        return None
    elif node.type == "block_comment" or \
       (node.children and node.children[0].type == "string"):
        return CommentNode([node])
    else:
        return None
        

#Used in remove_comments
def collect_comment_range(first_node, result_list, nodeIdSet, **kwargs):
    '''
    Asses if a node is a comment node, and appends tuples with the comment nodes start_byte and end_byte to the result_list

        Parameter:
            tree_sitter.Node
            List[Tuple[int,int]]

    '''
    comment_node = identify_comment_node(first_node, nodeIdSet)
    if comment_node :
        start_byte = comment_node.start_byte
        end_byte = comment_node.end_byte
        result_list.append((start_byte,end_byte))

def collect_comment_lines(first_node, result_list, nodeIdSet, **kwargs):
    '''
    Asses if a node is a comment node, and appends tuples with the comment nodes start_byte and end_byte to the result_list

        Parameter:
            tree_sitter.Node
            List[Tuple[int,int]]

    '''
    comment_node = identify_comment_node(first_node, nodeIdSet)
    if comment_node :
        # start_byte = comment_node.start_byte
        # end_byte = comment_node.end_byte
        # result_list.append((start_byte,end_byte))
        for line in range(comment_node.start_point[0], comment_node.end_point[0]+1):
            result_list.append(line)

#Used in agent
def collect_code_comment_range(func_node, first_node, content, result_list, nodeIdSet, **kwargs):
    """
    Extracts the source code of a function and any associated comment,
    then appends this data along with byte range information to the result list.

    If no comment is found, an empty string is used for the comment, and the byte
    range is set to the start of the function block.

    Parameters:
        func_node (tree_sitter.Node): The syntax node representing the function.
        first_node (tree_sitter.Node): The first node in function block, possibly a comment or string.
        content (str): The full source code text.
        result_list (List[Tuple[str, str, int, int]]): A list to which the tuple
            (function source code, associated comment, start byte, end byte) will be appended.

    Returns:
        None
    """
    code = content[func_node.start_byte : func_node.end_byte].strip()
    old_comment = ""
    last_node_before_block = first_node.parent.prev_sibling
    _ , start_col = func_node.start_point
    start_row, _ = last_node_before_block.end_point
    # Calculating the start_point to right under and one index in from the func def.
    start_row = start_row
    start_col = start_col+4
    start_byte = point_to_byte(content.encode("utf-8"),start_row,start_col)
    end_byte = start_byte
    comment_node = identify_comment_node(first_node, nodeIdSet)
    if comment_node:
        end_byte = comment_node.end_byte
        old_comment = content[first_node.parent.start_byte:end_byte] #first_node.parent.start_byte, is the first non whitespace in the block of the function, which should be the function-level comment
    result_list.append((code, old_comment, start_byte, end_byte))

#Used in semantic
def collect_code_comment_pairs(func_node, first_node, content, result_list, nodeIdSet, **kwargs):
    """
    Appends a record containing the source code of a function and its associated comment
    to the result_list, if a valid comment is found at start of function block.

    The comment must be of type 'comment', 'block_comment', or a string literal node.

    Parameters:
        func_node (tree_sitter.Node): The syntax node representing the function.
        first_node (tree_sitter.Node): The first node in function block, potentially a comment or string.
        content (str): The full source code as a string.
        result_list (List[Tuple[str, str]]): A list to which the tuple
            (function source code, associated comment) will be appended.

    Returns:
        None
    """
    func_def = " ".join(child.text.decode("utf-8") for child in func_node.children[:2])
    code = func_def
    old_comment = ""
    comment_node = identify_comment_node(first_node, nodeIdSet)
    if comment_node:
        old_comment = content[comment_node.start_byte: comment_node.end_byte]
        result_list.append((old_comment, code))

def collect_comment_change_lines(first_node, nodeIdSet, mod_lines, result_list, **kwargs):

    first_node.end_point[0]
    comment_node = identify_comment_node(first_node, nodeIdSet)
    if comment_node:
        for line in mod_lines:
            if line in range(comment_node.start_point[0],comment_node.end_point[0]+1):
                result_list.append(line)
    

def point_to_byte(source: bytes, row: int, col: int) -> int:
    '''
    Converts points(row,col) to bytes based on a given source (str)
    '''
    lines = source.splitlines(keepends=True)
    byte_offset = sum(len(lines[i]) for i in range(row+1)) + col
    return byte_offset

def edit_diff_restore_comments(file_language, head_content, cleaned_content):
    mod_comment_lines = set(extract_data(True, file_language,head_content, cleaned_content, collect_comment_change_lines, build_tree_from_head_content=True))
    diff = difflib.ndiff(head_content.splitlines(), cleaned_content.splitlines())
    diff_list = list(diff)
    counter = 1
    mod_diff = []
    for line in diff_list:
        if counter in mod_comment_lines and line.startswith("-"):
            mod_diff.append(" " + line[1:])
        elif line.startswith(" "):
            mod_diff.append(line)
        elif line.startswith("+") or line.startswith("?"):
            mod_diff.append(line)
            continue
        counter = counter + 1 
    
    modified_file = difflib.restore(mod_diff, 2)
    modified_file_str = "\n".join(modified_file)

    return modified_file_str
