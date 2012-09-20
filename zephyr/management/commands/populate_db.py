from django.core.management.base import BaseCommand
from django.utils.timezone import utc

from django.contrib.auth.models import User
from zephyr.models import Zephyr, UserProfile, ZephyrClass, Recipient, \
    Subscription, Huddle, get_huddle, Realm, UserMessage, get_user_profile_by_id
from zephyr.mit_subs_list import subs_list
from zephyr.lib.parallel import run_parallel
from django.db import transaction

import datetime
import random
from optparse import make_option

def create_users(name_list):
    for name, email in name_list:
        (username, domain) = email.split("@")
        if User.objects.filter(username=username):
            # We're trying to create the same user twice!
            raise
        realm = Realm.objects.get(domain=domain)
        user = User.objects.create_user(username=username, password=username,
                                        email=username+"@humbughq.com")
        user.save()
        UserProfile.create(user, realm, name, username)

def create_classes(class_list, realm):
    for name in class_list:
        if ZephyrClass.objects.filter(name=name, realm=realm):
            # We're trying to create the same zephyr class twice!
            raise
        ZephyrClass.create(name, realm)

class Command(BaseCommand):
    help = "Populate a test database"

    option_list = BaseCommand.option_list + (
        make_option('-n', '--num-zephyrs',
                    dest='num_zephyrs',
                    type='int',
                    default=600,
                    help='The number of zephyrs to create.'),
        make_option('--extra-users',
                    dest='extra_users',
                    type='int',
                    default=0,
                    help='The number of extra users to create'),
        make_option('--huddles',
                    dest='num_huddles',
                    type='int',
                    default=3,
                    help='The number of huddles to create.'),
        make_option('--personals',
                    dest='num_personals',
                    type='int',
                    default=6,
                    help='The number of personal pairs to create.'),
        make_option('--threads',
                    dest='threads',
                    type='int',
                    default=10,
                    help='The number of threads to use.'),
        make_option('--percent-huddles',
                    dest='percent_huddles',
                    type='float',
                    default=15,
                    help='The percent of messages to be huddles.'),
        make_option('--percent-personals',
                    dest='percent_personals',
                    type='float',
                    default=15,
                    help='The percent of messages to be personals.'),
        make_option('--stickyness',
                    dest='stickyness',
                    type='float',
                    default=20,
                    help='The percent of messages to repeat recent folks.'),
        make_option('--nodelete',
                    action="store_false",
                    default=True,
                    dest='delete',
                    help='Whether to delete all the existing messages.'),

        )

    def handle(self, **options):
        if options["percent_huddles"] + options["percent_personals"] > 100:
            self.stderr.write("Error!  More than 100% of messages allocated.\n")
            return

        class_list = ["Verona", "Denmark", "Scotland", "Venice", "Rome"]

        if options["delete"]:
            for klass in [Zephyr, ZephyrClass, UserProfile, User, Recipient,
                          Realm, Subscription, Huddle, UserMessage]:
                klass.objects.all().delete()

            # Create a test realm
            humbug_realm = Realm(domain="humbughq.com")
            humbug_realm.save()

            # Create test Users (UserProfiles are automatically created,
            # as are subscriptions to the ability to receive personals).
            names = [("Othello, the Moor of Venice", "othello@humbughq.com"), ("Iago", "iago@humbughq.com"),
                     ("Prospero from The Tempest", "prospero@humbughq.com"),
                     ("Cordelia Lear", "cordelia@humbughq.com"), ("King Hamlet", "hamlet@humbughq.com")]
            for i in xrange(options["extra_users"]):
                names.append(('Extra User %d' % (i,), 'extrauser%d' % (i,)))

            create_users(names)

            # Create public classes.
            create_classes(class_list, humbug_realm)
            recipient_classes = [klass.type_id for klass in
                                 Recipient.objects.filter(type=Recipient.CLASS)]

            # Create subscriptions to classes
            profiles = UserProfile.objects.all()
            for i, profile in enumerate(profiles):
                # Subscribe to some classes.
                for recipient in recipient_classes[:int(len(recipient_classes) *
                                                        float(i)/len(profiles)) + 1]:
                    r = Recipient.objects.get(type=Recipient.CLASS, type_id=recipient)
                    new_subscription = Subscription(userprofile=profile,
                                                    recipient=r)
                    new_subscription.save()
        else:
            humbug_realm = Realm.objects.get(domain="humbughq.com")
            recipient_classes = [klass.type_id for klass in
                                 Recipient.objects.filter(type=Recipient.CLASS)]

        # Extract a list of all users
        users = [user.id for user in User.objects.all()]

        # Create several initial huddles
        for i in xrange(options["num_huddles"]):
            get_huddle(random.sample(users, random.randint(3, 4)))

        # Create several initial pairs for personals
        personals_pairs = [random.sample(users, 2)
                           for i in xrange(options["num_personals"])]

        threads = options["threads"]
        jobs = []
        for i in xrange(threads):
            count = options["num_zephyrs"] / threads
            if i < options["num_zephyrs"] % threads:
                count += 1
            jobs.append((count, personals_pairs, options, self.stdout.write))
        for status, job in run_parallel(send_zephyrs, jobs, threads=threads):
            pass

        if options["delete"]:
            mit_realm = Realm(domain="mit.edu")
            mit_realm.save()

            # Create internal users
            internal_users = []
            create_users(internal_users)

            create_classes(subs_list, mit_realm)

            # Now subscribe everyone to these classes
            profiles = UserProfile.objects.filter(realm=mit_realm)
            for cls in subs_list:
                zephyr_class = ZephyrClass.objects.get(name=cls, realm=mit_realm)
                recipient = Recipient.objects.get(type=Recipient.CLASS, type_id=zephyr_class.id)
                for i, profile in enumerate(profiles):
                    # Subscribe to some classes.
                    new_subscription = Subscription(userprofile=profile, recipient=recipient)
                    new_subscription.save()

            self.stdout.write("Successfully populated test database.\n")

recipient_hash = {}
def get_recipient_by_id(rid):
    if rid in recipient_hash:
        return recipient_hash[rid]
    return Recipient.objects.get(id=rid)

# Create some test zephyrs, including:
# - multiple classes
# - multiple instances per class
# - multiple huddles
# - multiple personals converastions
# - multiple zephyrs per instance
# - both single and multi-line content
def send_zephyrs(data):
    (tot_zephyrs, personals_pairs, options, output) = data
    from django.db import connection
    connection.close()
    texts = file("zephyr/management/commands/test_zephyrs.txt", "r").readlines()
    offset = 0

    recipient_classes = [klass.id for klass in
                         Recipient.objects.filter(type=Recipient.CLASS)]
    recipient_huddles = [h.id for h in Recipient.objects.filter(type=Recipient.HUDDLE)]

    huddle_members = {}
    for h in recipient_huddles:
        huddle_members[h] = [s.userprofile.id for s in
                             Subscription.objects.filter(recipient_id=h)]

    num_zephyrs = 0
    random_max = 1000000
    recipients = {}
    while num_zephyrs < tot_zephyrs:
      with transaction.commit_on_success():
        saved_data = ''
        new_zephyr = Zephyr()
        length = random.randint(1, 5)
        lines = (t.strip() + '<br />' for t in texts[offset: offset + length])
        new_zephyr.content = '<p>' + ''.join(lines) + '</p>'
        offset += length
        offset = offset % len(texts)

        randkey = random.randint(1, random_max)
        if (num_zephyrs > 0 and
            random.randint(1, random_max) * 100. / random_max < options["stickyness"]):
            # Use an old recipient
            zephyr_type, recipient_id, saved_data = recipients[num_zephyrs - 1]
            if zephyr_type == Recipient.PERSONAL:
                personals_pair = saved_data
                random.shuffle(personals_pair)
            elif zephyr_type == Recipient.CLASS:
                new_zephyr.instance = saved_data
                new_zephyr.recipient = get_recipient_by_id(recipient_id)
            elif zephyr_type == Recipient.HUDDLE:
                new_zephyr.recipient = get_recipient_by_id(recipient_id)
        elif (randkey <= random_max * options["percent_huddles"] / 100.):
            zephyr_type = Recipient.HUDDLE
            new_zephyr.recipient = get_recipient_by_id(random.choice(recipient_huddles))
        elif (randkey <= random_max * (options["percent_huddles"] + options["percent_personals"]) / 100.):
            zephyr_type = Recipient.PERSONAL
            personals_pair = random.choice(personals_pairs)
            random.shuffle(personals_pair)
        elif (randkey <= random_max * 1.0):
            zephyr_type = Recipient.CLASS
            new_zephyr.recipient = get_recipient_by_id(random.choice(recipient_classes))

        if zephyr_type == Recipient.HUDDLE:
            sender_id = random.choice(huddle_members[new_zephyr.recipient.id])
            new_zephyr.sender = get_user_profile_by_id(sender_id)
        elif zephyr_type == Recipient.PERSONAL:
            new_zephyr.recipient = Recipient.objects.get(type=Recipient.PERSONAL,
                                                         type_id=personals_pair[0])
            new_zephyr.sender = get_user_profile_by_id(personals_pair[1])
            saved_data = personals_pair
        elif zephyr_type == Recipient.CLASS:
            zephyr_class = ZephyrClass.objects.get(id=new_zephyr.recipient.type_id)
            # Pick a random subscriber to the class
            new_zephyr.sender = random.choice(Subscription.objects.filter(
                    recipient=new_zephyr.recipient)).userprofile
            new_zephyr.instance = zephyr_class.name + str(random.randint(1, 3))
            saved_data = new_zephyr.instance

        new_zephyr.pub_date = datetime.datetime.utcnow().replace(tzinfo=utc)
        new_zephyr.save()

        recipients[num_zephyrs] = [zephyr_type, new_zephyr.recipient.id, saved_data]
        num_zephyrs += 1
    return tot_zephyrs
