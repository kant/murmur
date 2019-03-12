from __future__ import unicode_literals, division

import logging
import Queue
import sys
import typing as t  # noqa: F401 ignore unused we use it for typing
import ujson
import random
from StringIO import StringIO
from email import message
from imapclient import IMAPClient  # noqa: F401 ignore unused we use it for typing

from browser.models.event_data import NewMessageData
from browser.models.mailbox import MailBox  # noqa: F401 ignore unused we use it for typing
from schema.youps import Action, TaskScheduler  # noqa: F401 ignore unused we use it for typing
from smtp_handler.utils import codeobject_dumps, is_gmail, send_email

logger = logging.getLogger('youps')  # type: logging.Logger


def interpret(mailbox, code, is_simulate=False):
    # type: (MailBox, unicode, bool) -> t.Dict[t.AnyStr, t.Any]

    # assert we actually got a mailbox
    assert isinstance(mailbox, MailBox)
    # assert the code is unicode
    assert isinstance(code, unicode)
    assert mailbox.new_message_handler is not None

    # set up the default result
    res = {'status': True, 'imap_error': False, 'imap_log': ""}

    # get the logger for user output
    userLogger = logging.getLogger('youps.user')  # type: logging.Logger
    # get the stream handler associated with the user output
    userLoggerStreamHandlers = filter(lambda h: isinstance(h, logging.StreamHandler), userLogger.handlers)
    userLoggerStream = userLoggerStreamHandlers[0].stream if userLoggerStreamHandlers else None
    assert userLoggerStream is not None

    # create a string buffer to store stdout
    user_std_out = StringIO()

    # define user methods
    def create_draft(subject="", to_addr="", cc_addr="", bcc_addr="", body="", draft_folder="Drafts"):
        new_message = message.Message()
        new_message["Subject"] = subject
            
        if type(to_addr) == 'list':
            to_addr = ",".join(to_addr)
        if type(cc_addr) == 'list':
            cc_addr = ",".join(cc_addr)
        if type(bcc_addr) == 'list':
            bcc_addr = ",".join(bcc_addr)
            
        new_message["To"] = to_addr
        new_message["Cc"] = cc_addr
        new_message["Bcc"] = bcc_addr
        new_message.set_payload(body) 
            
        if not is_simulate:
            if is_gmail(mailbox._imap_client):
                mailbox._imap_client.append('[Gmail]/Drafts', str(new_message))
                
            else:
                try:
                    # if this imap service provides list capability takes advantage of it
                    if [l[0][0] for l in mailbox._imap_client.list_folders()].index('\\Drafts'):
                        mailbox._imap_client.append(mailbox._imap_client.list_folders()[2][2], str(new_message))
                except Exception as e:
                    # otherwise try to guess a name of draft folder
                    try:
                        mailbox._imap_client.append('Drafts', str(new_message))
                    except IMAPClient.Error, e:
                        try:
                            mailbox._imap_client.append('Draft', str(new_message))
                        except IMAPClient.Error, e:
                            if "append failed" in e:
                                mailbox._imap_client.append(draft_folder, str(new_message))

        logger.debug("create_draft(): Your draft %s has been created" % subject)

    def create_folder(folder_name):
        if not is_simulate: 
            mailbox._imap_client.create_folder( folder_name )

        logger.debug("create_folder(): A new folder %s has been created" % folder_name)

    def rename_folder(old_name, new_name):
        if not is_simulate: 
            mailbox._imap_client.rename_folder( old_name, new_name )

        logger.debug("rename_folder(): Rename a folder %s to %s" % (old_name, new_name))

    def on_message_arrival(func):
        mailbox.new_message_handler += func

    from http_handler.tasks import add_periodic_task

    def set_interval(interval=None, func=None):
        if not interval:
            raise Exception('set_interval(): requires interval (in minute)')

        if interval < 1:
            raise Exception('set_interval(): requires interval larger than 1 minute')

        if not func or type(func).__name__ != "function":
            raise Exception('set_interval(): requires callback function but it is %s ' % type(func).__name__)

        if func.func_code.co_argcount != 0:
            raise Exception('set_interval(): your callback function should have only 0 argument, but there are %d argument(s)' % func.func_code.co_argcount)

        action = Action(trigger="interval", code=codeobject_dumps(func.func_code), mode=mailbox._imap_account.current_mode)
        action.save()
        add_periodic_task.apply_async(args=[interval, action.id], queue='default', routing_key='default.import')   

        code = 'a=Action.objects.get(id=%d)\ncode_object=codeobject_loads(a.code)\ng = type(codeobject_loads)(code_object ,locals())\ng()' % action.id
        args = ujson.dumps( [mailbox._imap_account.id, code] )

        ptask_name = "%d_set_interval-%d" % (int(mailbox._imap_account), random.randint(1, 10000))
        TaskScheduler.schedule_every('run_interpret', 'minutes', interval, ptask_name, args)

        mailbox._imap_account.status_msg = mailbox._imap_account.status_msg + "[%s]set_interval(): executing every %d minutes \n" % (ptask_name, interval)
        mailbox._imap_account.save()

    def send(subject="", to="", body="", smtp=""):  # TODO add "cc", "bcc"
        if len(to) == 0:
            raise Exception('send(): recipient email address is not provided')

        if not is_simulate:
            send_email(subject, mailbox._imap_account.email, to, body)
        logger.debug("send(): sent a message to  %s" % str(to))

    # execute user code
    try:
        # set the stdout to a string
        sys.stdout = user_std_out

        # set the user logger to
        userLoggerStream = user_std_out

        # define the variables accessible to the user
        user_environ = {
            'create_draft': create_draft,
            'create_folder': create_folder,
            'new_message_handler': mailbox.new_message_handler,
            'on_message_arrival': on_message_arrival,
            'set_interval': set_interval
        }

        # execute the user's code
        exec(code, user_environ)

        # fire new message events
        while True:
            try:
                event_data = mailbox.event_data_queue.get_nowait()
                if isinstance(event_data, NewMessageData):
                    event_data.fire_event(mailbox.new_message_handler)
            except Queue.Empty:
                break

    except Exception:
        res['status'] = False
        userLogger.exception("failure running user %s code" %
                             mailbox._imap_account.email)
    finally:
        # set the stdout back to what it was
        sys.stdout = sys.__stdout__
        userLoggerStream = sys.__stdout__
        res['imap_log'] = user_std_out.getvalue() + res['imap_log']
        user_std_out.close()
        return res

    # with stdoutIO() as s:
    #     def catch_exception(e):
    #         etype, evalue = sys.exc_info()[:2]
    #         estr = traceback.format_exception_only(etype, evalue)
    #         logstr = 'Error during executing your code \n'
    #         for each in estr:
    #             logstr += '{0}; '.format(each.strip('\n'))

    #         logstr = "%s \n %s" % (logstr, str(e))

    #         # Send this error msg to the user
    #         res['imap_log'] = logstr
    #         res['imap_error'] = True

    #     def on_message_arrival(func=None):
    #         if not func or type(func).__name__ != "function":
    #             raise Exception('on_message_arrival(): requires callback function but it is %s ' % type(func).__name__)

    #         if func.func_code.co_argcount != 1:
    #             raise Exception('on_message_arrival(): your callback function should have only 1 argument, but there are %d argument(s)' % func.func_code.co_argcount)

    #         # TODO warn users if it conatins send() and their own email (i.e., it potentially leads to infinite loops)

    #         # TODO replace with the right folder
    #         current_folder_schema = FolderSchema.objects.filter(imap_account=imap_account, name="INBOX")[0]
    #         action = Action(trigger="arrival", code=codeobject_dumps(func.func_code), folder=current_folder_schema)
    #         action.save()

    #     def set_timeout(delay=None, func=None):
    #         if not delay:
    #             raise Exception('set_timeout(): requires delay (in second)')

    #         if delay < 1:
    #             raise Exception('set_timeout(): requires delay larger than 1 sec')

    #         if not func:
    #             raise Exception('set_timeout(): requires code to be executed periodically')

    #         args = ujson.dumps( [imap_account.id, marshal.dumps(func.func_code), search_creteria, is_test, email_content] )
    #         add_periodic_task.delay( delay, args, delay * 2 - 0.5 ) # make it expire right before 2nd execution happens



    #     # return a list of email UIDs
    #     def search(criteria=u'ALL', charset=None, folder=None):
    #         # TODO how to deal with folders
    #         # iterate through all the functions
    #         if folder is None:
    #             pass

    #         # iterate through a folder of list of folder
    #         else:
    #             # if it's a list iterate
    #             pass
    #             # else it's a string search a folder

    #         select_folder('INBOX')
    #         return imap.search(criteria, charset)



    #     def delete_folder(folder):
    #         pile.delete_folder(folder, is_test)

    #     def list_folders(directory=u'', pattern=u'*'):
    #         return pile.list_folders(directory, pattern)

    #     def select_folder(folder):
    #         if not imap.folder_exists(folder):
    #             logger.error("Select folder; folder %s not exist" % folder)
    #             return

    #         imap.select_folder(folder)
    #         logger.debug("Select a folder %s" % folder)

    #     def get_mode():
    #         if imap_account.current_mode:
    #             return imap_account.current_mode.uid
    #         else:
    #             return None

    #     def set_mode(mode_index):
    #         try:
    #             mode_index = int(mode_index)
    #         except ValueError:
    #             raise Exception('set_mode(): args mode_index must be a index (integer)')

    #         mm = MailbotMode.objects.filter(uid=mode_index, imap_account=imap_account)
    #         if mm.exists():
    #             mm = mm[0]
    #             if not is_test:
    #                 imap_account.current_mode = mm
    #                 imap_account.save()

    #             logger.debug("Set mail mode to %s (%d)" % (mm.name, mode_index))
    #             return True
    #         else:
    #             logger.error("A mode ID %d not exist!" % (mode_index))
    #             return False

    #     try:
    #         if is_valid:
    #             exec code in globals(), locals()
    #             pile.add_flags(['YouPS'])
    #             res['status'] = True
    #     except Action.DoesNotExist:
    #         logger.debug("An action is not existed right now. Maybe you change your script after this action was added to the queue.")
    #     except Exception as e:
    #         catch_exception(e)

    #     res['imap_log'] = s.getvalue() + res['imap_log']

    #     return res